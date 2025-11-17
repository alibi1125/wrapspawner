# Copyright (c) Alexander Birk.
# Original copyright (c) Jupyter Development Team.
# Distributed under the terms of the Modified BSD License.

version_info = (
    1,
    1,
    1,
    'dev3', # comment-out this line for a release
)
__version__ = '.'.join(map(str, version_info[:3]))

if len(version_info) > 3:
    __version__ = '%s-%s' % (__version__, version_info[3])
