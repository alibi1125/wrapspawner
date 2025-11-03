# Copyright (c) Regents of the University of Minnesota
# Distributed under the terms of the Modified BSD License.

"""WrapSpawner classes

WrapSpawner provides a mechanism to wrap the interface of a Spawner such that
the Spawner class to use for single-user servers can be chosen dynamically.
The child Spawner is created and started using the same logic as in User.spawn(),
but not until start() or load_state() is called. Thus, subclasses may modify the
class or properties of the child Spawner at any earlier point (e.g. from
Authenticator pre_spawn hooks or options form processing).

Note that there is no straightforward way to simply save the child_class Type
value in the ORM, so a subclass that provides a way to change the child_class
must also arrange to reproduce that change when state is reloaded.

Provided as an initial application is ProfilesSpawner, which accepts a list
of provided Spawner configurations, and generates an options form from that list.
Using this mechanism, the administrator can provide users with a pre-approved
selection of Spawner configurations.
"""

import json
import re
import urllib.request

from tornado import concurrent
from tornado.httpclient import AsyncHTTPClient, HTTPRequest, HTTPClientError
from tornado.ioloop import IOLoop

from jupyterhub.spawner import LocalProcessSpawner, Spawner
from jupyterhub.utils import maybe_future
from traitlets import (
    Instance, Type, Tuple, List, Dict, Unicode, Any
)
from traitlets import directional_link, validate, TraitError, default

import pwd
import json
import os
import subprocess

from .filters import ProfilesFilter, DummyFilter

# Only needed for DockerProfilesSpawner
try:
    import docker
except ImportError:
    pass


# Utility to create dummy Futures to return values through yields
def _yield_val(x=None):
    f = concurrent.Future()
    f.set_result(x)
    return f

class WrapSpawner(Spawner):

    # Grab this from constructor args in case some Spawner ever wants it
    config = Any()

    child_class = Type(LocalProcessSpawner,
        Spawner,
        config=True,
        help="""The class to wrap for spawning single-user servers.
                Should be a subclass of Spawner.
                """
        )

    child_config = Dict(default_value={},
        config=True,
        help="Dictionary of config values to apply to wrapped spawner class."
        )

    child_state = Dict(default_value={})

    child_spawner = Instance(Spawner, allow_none=True)

    def construct_child(self):
        if self.child_spawner is None:
            self.child_spawner = self.child_class(
                user = self.user,
                db   = self.db,
                hub  = self.hub,
                authenticator = self.authenticator,
                oauth_client_id = self.oauth_client_id,
                cookie_options = self.cookie_options,
                orm_spawner = self.orm_spawner,
                server = self._server,
                config = self.config,
                **self.child_config
                )
            # initial state will always be wrong since it will see *our* state
            self.child_spawner.clear_state()
            if self.child_state:
                self.child_spawner.load_state(self.child_state)

            # link traits common between self and child
            common_traits = (
                set(self.trait_names()) &
                set(self.child_spawner.trait_names()) -
                set(self.child_config.keys())
            )
            for trait in common_traits:
                directional_link((self, trait), (self.child_spawner, trait))
        return self.child_spawner

    def load_child_class(self, state):
        # Subclasses must arrange for correct child_class setting from load_state
        pass

    def load_state(self, state):
        super().load_state(state)
        self.load_child_class(state)
        self.child_config.update(state.get('child_conf', {}))
        self.child_state = state.get('child_state', {})
        self.construct_child()

    def get_state(self):
        state = super().get_state()
        state['child_conf'] = self.child_config
        if self.child_spawner:
            self.child_state = state['child_state'] = self.child_spawner.get_state()
        return state

    def clear_state(self):
        super().clear_state()
        if self.child_spawner:
            self.child_spawner.clear_state()
        self.child_state = {}
        self.child_config = {}
        self.child_spawner = None

    # proxy functions for start/poll/stop
    # pass back the child's Future, or create a dummy if needed

    def start(self):
        if not self.child_spawner:
            self.construct_child()
        return self.child_spawner.start()

    def stop(self, now=False):
        if self.child_spawner:
            return self.child_spawner.stop(now)
        else:
            return _yield_val()

    def poll(self):
        if self.child_spawner:
            return self.child_spawner.poll()
        else:
            return _yield_val(1)

    def move_certs(self, paths):
        return self.child_spawner.move_certs(paths)

    def run_pre_spawn_hook(self):
        # Run wrapspawner`s own hook first if defined
        if self.pre_spawn_hook is not None:
            return self.pre_spawn_hook(self)
        return self.child_spawner.run_pre_spawn_hook()

    def run_post_stop_hook(self):
        # Run wrapspawner`s own hook first if defined
        try:
            if self.post_stop_hook is not None:
                self.post_stop_hook(self)
                return self.child_spawner.run_post_stop_hook()
        except Exception:
            self.log.exception("post_stop_hook failed with exception: %s", self)

    async def run_auth_state_hook(self, auth_state):
        # Run wrapspawner`s own hook first if defined
        if self.auth_state_hook is not None:
            await maybe_future(self.run_auth_state_hook(self, auth_state))
        # run_auth_state_hook is the first hook that gets called when starting, so it needs to be able to construct a child
        if not self.child_spawner:
            self.construct_child()
        await self.child_spawner.run_auth_state_hook(auth_state)

    if hasattr(Spawner, 'progress'):
        @property
        def progress(self):
            if self.child_spawner:
                return self.child_spawner.progress
            else:
                raise RuntimeError("No child spawner yet exists - can not get progress yet")

class ProfilesSpawner(WrapSpawner):

    """ProfilesSpawner - leverages the Spawner options form feature to allow user-driven
        configuration of Spawner classes while permitting:
        1) configuration of Spawner classes that don't natively implement options_form
        2) administrator control of allowed configuration changes
        3) runtime choice of which Spawner backend to launch
    """

    profiles = List(
        trait = Tuple( Unicode(), Unicode(), Type(Spawner), Dict() ),
        default_value = [ ( 'Local Notebook Server', 'local', LocalProcessSpawner,
                            {'start_timeout': 15, 'http_timeout': 10} ) ],
        minlen = 1,
        config = True,
        help = """List of profiles to offer for selection. Signature is:
            List(Tuple( Unicode, Unicode, Type(Spawner), Dict )) corresponding to
            profile display name, unique key, Spawner class, dictionary of spawner config options.

            The first three values will be exposed in the input_template as {display}, {key}, and {type}"""
        )

    @validate("profiles")
    def _validate_profiles(self, proposal):
        profiles = proposal.value

        seen = set()
        duplicated = {p[1] for p in profiles if p[1] in seen or seen.add(p[1])}
        if len(duplicated):
            raise TraitError(
                f"Invalid wrapspawner profiles, profiles keys are not unique : {duplicated}")

        return profiles

    child_profile = Unicode()

    form_template = Unicode(
        """<label for="profile">Select a job profile:</label>
        <select class="form-control" name="profile" required autofocus>
        {input_template}
        </select>
        """,
        config = True,
        help = """Template to use to construct options_form text. {input_template} is replaced with
            the result of formatting input_template against each item in the profiles list."""
        )

    first_template = Unicode('selected',
        config=True,
        help="Text to substitute as {first} in input_template"
        )

    input_template = Unicode("""
        <option value="{key}" {first}>{display}</option>""",
        config = True,
        help = """Template to construct {input_template} in form_template. This text will be formatted
            against each item in the profiles list, in order, using the following key names:
            ( display, key, type ) for the first three items in the tuple, and additionally
            first = "checked" (taken from first_template) for the first item in the list, so that
            the first item starts selected."""
        )

    def _options_form_default(self):
        temp_keys = [ dict(display=p[0], key=p[1], type=p[2], first='') for p in self.profiles ]
        temp_keys[0]['first'] = self.first_template
        text = ''.join([ self.input_template.format(**tk) for tk in temp_keys ])
        return self.form_template.format(input_template=text)

    def options_from_form(self, formdata):
        # Default to first profile if somehow none is provided
        return dict(profile=formdata.get('profile', [self.profiles[0][1]])[0])

    # load/get/clear : save/restore child_profile (and on load, use it to update child class/config)

    def select_profile(self, profile):
        # Select matching profile, or do nothing (leaving previous or default config in place)
        for p in self.profiles:
            if p[1] == profile:
                self.child_class = p[2]
                self.child_config = p[3]
                break

    def construct_child(self):
        self.child_profile = self.user_options.get('profile', "")
        self.select_profile(self.child_profile)
        super().construct_child()

    def load_child_class(self, state):
        try:
            self.child_profile = state['profile']
        except KeyError:
            self.child_profile = ''
        self.select_profile(self.child_profile)

    def get_state(self):
        state = super().get_state()
        state['profile'] = self.child_profile
        return state

    def clear_state(self):
        super().clear_state()
        self.child_profile = ''

class FilteredSpawner(ProfilesSpawner):

    """FilteredSpawner - leverages the Spawner options form feature to allow user-driven
        configuration of Spawner classes while permitting:
        1) configuration of Spawner classes that don't natively implement options_form
        2) administrator control of allowed configuration changes
        3) runtime choice of which Spawner backend to launch
        4) filtered selection of profiles depending on current user
    """

    default_profiles = List(
        trait = Tuple( Unicode(), Unicode(), Type(Spawner), Dict(), List(Unicode()) ),
        default_value = [ ( 'Local Notebook Server', 'local', LocalProcessSpawner,
                            {'start_timeout': 15, 'http_timeout': 10}, ['*'] ) ],
        minlen = 1,
        config = True,
        help = """List of profiles to offer for selection. Signature is:
            List(Tuple( Unicode, Unicode, Type(Spawner), Dict, Tuple(Unicode) )) corresponding to
            profile display name, unique key, Spawner class, dictionary of spawner config options,
            Tuple of allowed user groups (`*` is the wildcard for common profiles)

            The first three values will be exposed in the input_template as {display}, {key}, and {type}"""
        )

    filterclass = Type(
        klass = ProfilesFilter,
        default_value = DummyFilter,
        config = True
        )

    @property
    def profiles(self):
        return self.filterclass.apply_filter(self.default_profiles, self.user.name)

class ImportedProfilesSpawner(ProfilesSpawner):

    """ImportedProfilesSpawner - leverages the Spawner options form feature to allow user-driven
        configuration of Spawner classes while permitting:
        1) configuration of Spawner classes that don't natively implement options_form
        2) administrator control of allowed configuration changes
        3) runtime choice of which Spawner backend to launch
        4) loading the spawner profiles from disk, allowing for dynamic changes and per-user customization
    """

    common_profiles_loc = Unicode(
        "/etc/jupyterhub/common_profiles.json",
        config=True,
        help="Path to a JSON file containing common profiles, to be offered to every user."
    )

    home_base_dir = Unicode(
        "",
        config=True,
        help="If set, this is used as base of the home directory. Use to override the home directory " \
        "returned by getpwnam, e.g. for local users vs. LDAP users. User home directory default " \
        "path is then constructed as `home_base_dir + self.user.name`."
    )

    # Useful IF getpwnam on submit host returns correct info for exec host
    homedir = Unicode()

    @default("homedir")
    def _homedir_default(self):
        if self.home_base_dir == "":
            homedir = pwd.getpwnam(self.user.name).pw_dir
            self.log.debug(f"Using system home directory {homedir} for user {self.user.name}")
        else:
            homedir = os.path.join(self.home_base_dir, self.user.name)
            self.log.debug(f"Home dir overridden, using {homedir} for user {self.user.name}")
        return homedir

    user_subdir_path = Unicode(
        ".jupyterhub",
        config=True,
        help="Name of the subdirectory to put in the user's working directory. All files necessary "
        "for the Spawners' correct operation will be placed there.",
    )

    user_profiles_loc = Unicode()

    @default("user_profiles_loc")
    def _user_profiles_loc_default(self):
        # Construct path to user_profiles file
        user_profiles_file = 'user_profiles.json'
        full_path = os.path.join(self.homedir, self.user_subdir_path, user_profiles_file)
        self.log.debug(f"User profiles location is {full_path}")
        return full_path

    @property
    def profiles(self):
        # Get some facts about the requesting user
        user = pwd.getpwnam(self.user.name)
        uid = user.pw_uid
        gid = user.pw_gid

        profiles_data = []
        # Read common profiles
        try:
            with open(self.common_profiles_loc) as cprofiles_file:
                profiles_data += json.load(cprofiles_file)
        except FileNotFoundError:
            self.log.warn("JSON containing common profiles not found")
        except OSError:
            self.log.warn("JSON containing common profiles could not be read")
        # Read user profiles
        try:
            proc = subprocess.run(['cat', self.user_profiles_loc], check=True, capture_output=True, text=True, user=uid, group=gid)
            profiles_data += json.loads(proc.stdout)
        except subprocess.CalledProcessError as e:
            self.log.warn(f"Non-normal exit code when reading JSON containing user profiles. Code {e.returncode}")
        except OSError:
            self.log.error("Invalid arguments for reading user profiles")
        # If there are no profiles, log that.
        if len(profiles_data) == 0:
            self.log.error("No profiles collected. There will be nothing to spawn.")
        else:
            self.log.debug(f"Full profiles data looks as follows: {profiles_data}")
        # Construct a profiles list compatible with the ProfilesSpawner's default signature
        profiles_clean = []
        for index, profile in enumerate(profiles_data):
            # To keep our JSON files short, avoiding unnecessary info, there are only two non-optional keys per profile: description and options.
            # The unique name is built at runtime and spawner is assumed to be batchspawner.SlurmSpawner if it isn't provided.
            try:
                spawner = profile['spawner']
            except KeyError:
                spawner = "batchspawner.SlurmSpawner"
            profiles_clean.append(
                ( profile['description'], f"prof_{index}", spawner, profile['options'] )
            )
        return profiles_clean

    # Overload options_form default to make it callable. This ensures every time the spawner options site is re-rendered, the form gets updated.
    @default("options_form")
    def _options_form_default(self):
        def render_option_form(self):
            temp_keys = [ dict(display=p[0], key=p[1], type=p[2], first='') for p in self.profiles ]
            temp_keys[0]['first'] = self.first_template
            text = ''.join([ self.input_template.format(**tk) for tk in temp_keys ])
            return self.form_template.format(input_template=text)
        return render_option_form


def renew_api_token(func):
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except (AttributeError, HTTPClientError):
            args[0]._get_user_token()
            return func(*args, **kwargs)
        return wrapper

class ServiceProfilesSpawner(ProfilesSpawner):

    """ServiceProfilesSpawner - leverages the Spawner options form feature to allow user-driven
        configuration of Spawner classes while permitting:
        1) configuration of Spawner classes that don't natively implement options_form
        2) administrator control of allowed configuration changes
        3) runtime choice of which Spawner backend to launch
        4) loading the spawner profiles from disk, allowing for dynamic changes and per-user customization
    """

    profiles_service_url = Unicode(
        "http://127.0.0.1:8003/services/jupyterhub_profile_tool/profiles/data",
        config = True,
        help="URL under which the profile service serves the user profiles."
    )

    async def _get_user_token(self):
        http_client = AsyncHTTPClient()
        hub_api_url = self.hub.api_url.rstrip('/')
        req = HTTPRequest(
            url = f"{hub_api_url}/users/{self.user.name}/tokens",
            method = "POST",
            body = json.dumps({"note": f"spawner-init-{self.user.name}",
                               "expires_in": 300,
            }),
            headers = {"Authorization": f"token {self.hub.api_token}",
                       "Content-Type": "application/json",
            }
        )
        try:
            resp = await http_client.fetch(req)
            data = json.loads(resp.body.decode('utf-8'))
            self.user_token = data["token"]
            self.log.info(f"Temporary user token for user {self.user.name} created in ServiceProfilesSpawner")
            return
        except HTTPClientError as e:
            msg = e.response.body.decode() if e.response and e.response.body else str(e)
            self.log.error(f"Error creating temporary user token for user {self.user.name}: {e.code} {msg}")
        except KeyError as e:
            msg = e.response.body.decode() if e.response and e.response.body else str(e)
            self.log.error(f"No temporary user token was returned for user {self.user.name}: {msg}")
        except Exception as e:
            self.log.error(f"Unexpected error creating temporary user token for user {self.user.name}: {e}")
        self.user_token = ""
        return
    
    async def _get_profiles(self):
        self.log.debug("Next: Creating HTTP Client")
        http_client = AsyncHTTPClient()
        self.user_token = "2ac1ee9a8e2240d6b347e13f23f31ef1"
        self.log.debug("Next: Creating Request")
        req = HTTPRequest(
            url = self.profiles_service_url,
            headers = {"Authorization": f"token {self.user_token}",
                       "Accept": "application/json",
            },
        )
        try:
            return http_client.fetch(req)
        except HTTPClientError as e:
            msg = e.response.body.decode() if e.response and e.response.body else str(e)
            self.log.error(f"Profile information could not be fetched due to HTTP error {e.code}. The error was {msg}")

    @property
    # @renew_api_token
    def profiles(self):
        fetched = IOLoop.current().run_sync(self._get_profiles)
        profiles_data = json.loads(fetched.body.decode("utf-8"))["profiles"]
        profiles_clean = []
        for profile in profiles_data:
            # To keep our JSON files short, avoiding unnecessary info, there are only two non-optional keys per profile: description and options.
            # The unique name is built at runtime and spawner is assumed to be batchspawner.SlurmSpawner if it isn't provided.
            try:
                spawner = profile['spawner']
            except KeyError:
                spawner = "batchspawner.SlurmSpawner"
            profiles_clean.append(
                ( profile['description'], profile['profile_id'], spawner, profile['options'] )
            )
        return profiles_clean

    # Overload options_form default to make it callable. This ensures every time the spawner options site is re-rendered, the form gets updated.
    @default("options_form")
    def _options_form_default(self):
        def render_option_form(self):
            temp_keys = [ dict(display=p[0], key=p[1], type=p[2], first='') for p in self.profiles ]
            temp_keys[0]['first'] = self.first_template
            text = ''.join([ self.input_template.format(**tk) for tk in temp_keys ])
            return self.form_template.format(input_template=text)
        return render_option_form

class DockerProfilesSpawner(ProfilesSpawner):

    """DockerProfilesSpawner - leverages ProfilesSpawner to dynamically create DockerSpawner
        profiles dynamically by looking for docker images that end with "jupyterhub". Due to the
        profiles being dynamic the "profiles" config item from the ProfilesSpawner is renamed as
        "default_profiles". Please note that the "docker" and DockerSpawner packages are required
        for this spawner to work.
    """

    default_profiles = List(
        trait = Tuple( Unicode(), Unicode(), Type(Spawner), Dict() ),
        default_value = [],
        config = True,
        help = """List of profiles to offer in addition to docker images for selection. Signature is:
            List(Tuple( Unicode, Unicode, Type(Spawner), Dict )) corresponding to
            profile display name, unique key, Spawner class, dictionary of spawner config options.

            The first three values will be exposed in the input_template as {display}, {key}, and {type}"""
        )

    docker_spawner_args = Dict(
        default_value = {},
        config = True,
        help = "Args to pass to DockerSpawner."
    )

    jupyterhub_docker_tag_re = re.compile('^.*jupyterhub$')

    def _nvidia_args(self):
        try:
            resp = urllib.request.urlopen('http://localhost:3476/v1.0/docker/cli/json')
            body = resp.read().decode('utf-8')
            args =  json.loads(body)
            return dict(
                read_only_volumes={vol.split(':')[0]: vol.split(':')[1] for vol in args['Volumes']},
                extra_create_kwargs={"volume_driver": args['VolumeDriver']},
                extra_host_config={"devices": args['Devices']},
            )
        except urllib.error.URLError:
            return {}


    def _docker_profile(self, nvidia_args, image):
        spawner_args = dict(container_image=image, network_name=self.user.name)
        spawner_args.update(self.docker_spawner_args)
        spawner_args.update(nvidia_args)
        nvidia_enabled = "w/GPU" if len(nvidia_args) > 0 else "no GPU"
        return ("Docker: (%s): %s"%(nvidia_enabled, image), "docker-%s"%(image), "dockerspawner.SystemUserSpawner", spawner_args)

    def _jupyterhub_docker_tags(self):
        try:
            include_jh_tags = lambda tag: self.jupyterhub_docker_tag_re.match(tag)
            return filter(include_jh_tags, [tag for image in docker.from_env().images.list() for tag in image.tags])
        except NameError:
            raise Exception('The docker package is not installed and is a dependency for DockerProfilesSpawner')

    def _docker_profiles(self):
        return [self._docker_profile(self._nvidia_args(), tag) for tag in self._jupyterhub_docker_tags()]

    @property
    def profiles(self):
        return self.default_profiles + self._docker_profiles()

    @property
    def options_form(self):
        temp_keys = [ dict(display=p[0], key=p[1], type=p[2], first='') for p in self.profiles]
        temp_keys[0]['first'] = self.first_template
        text = ''.join([ self.input_template.format(**tk) for tk in temp_keys ])
        return self.form_template.format(input_template=text)


# vim: set ai expandtab softtabstop=4:
