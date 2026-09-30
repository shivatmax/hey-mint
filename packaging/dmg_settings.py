# dmgbuild settings for the Hey Mint disk image (packaging/build_app.sh runs:
#   dmgbuild -s packaging/dmg_settings.py -D app=... -D link=... -D background=... -D icon=... "Hey Mint" out.dmg)
# Positions match the background art drawn by make_dmg_background.py.
import os

app = defines["app"]              # noqa: F821  (dmgbuild provides `defines`)
link = defines["link"]            # noqa: F821  (a .webloc: the page with the install command)
background = defines["background"]  # noqa: F821
icon = defines.get("icon")        # noqa: F821

format = "UDZO"
size = None
files = [app, link]
symlinks = {"Applications": "/Applications"}
badge_icon = None
if icon and os.path.exists(icon):
    badge_icon = None
    icon = icon
else:
    icon = None

background = background
show_status_bar = False
show_tab_view = False
show_toolbar = False
show_pathbar = False
show_sidebar = False
default_view = "icon-view"
include_icon_view_settings = "auto"
window_rect = ((240, 160), (660, 440))
icon_size = 96
text_size = 13
arrange_by = None
label_pos = "bottom"
icon_locations = {
    os.path.basename(app): (150, 160),
    "Applications": (510, 160),
    os.path.basename(link): (556, 322),
    # Hidden files (dmgbuild's background and volume icon) stay out of sight even with "show hidden files" on.
    ".background.tiff": (1400, 1400),
    ".VolumeIcon.icns": (1400, 1500),
    ".fseventsd": (1400, 1600),
}
