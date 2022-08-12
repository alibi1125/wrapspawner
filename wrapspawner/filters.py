import warnings
from traitlets.config import LoggingConfigurable

try:
    import grp
except ModuleNotFoundError:
    warnings.warn("Python builtin 'grp' unavailable. Filters relying on it "
                  "cannot be used!")

class ProfilesFilter(LoggingConfigurable):
    
    def perform_filter(default_profiles, user):
        pass
        
class DummyFilter(ProfilesFilter):

    def perform_filter(default_profiles, user):
        return [x[:4] for x in default_profiles]

class UnixGroupFilter(ProfilesFilter):

    def perform_filter(default_profiles, user):
        profiles = []
        for p in default_profiles:
            for group in p[4]:
                # Stop early if the group spec is the wildcard `*`.
                if group == '*':
                    profiles.append(p)
                    break
                try:
                    # grp.getgrnam returns a struct that includes a list
                    # containing the names of all group members as its 3rd
                    # entry.
                    members = grp.getgrnam(group)[3]
                except KeyError:
                    # We land here if the group could not be found.  Warn
                    # about the incident but, apart from that, continue.\
                    self.log.warn("Encountered unknown group %s. Ignoring...".format(group))
                    members = []
                # We can stop scanning groups when we found the user in one
                # of them.
                if user in members:
                    profiles.append(p[:4])
                    break
        return profiles