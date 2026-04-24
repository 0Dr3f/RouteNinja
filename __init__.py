"""RouteNinja plugin entry point.

Wires the core RouteNinja engine to Binary Ninja's plugin commands and
sidebar widget. Exposes a module-level `route_ninja` singleton via builtins
so other plugins / the scripting console can poke at it.
"""
import builtins
import logging

from RouteNinja.route_ninja import RouteNinja
from RouteNinja.gui_wrapper import GUIWrapper

logging.basicConfig(
    level=logging.DEBUG,
    format="[RouteNinja] %(levelname)s: %(message)s",
)
logging.debug("RouteNinja plugin loaded.")

route_ninja = RouteNinja()

wrapper = GUIWrapper(route_ninja)
wrapper.register_plugin_commands()
wrapper.register_sidebar_widget()

builtins.route_ninja = route_ninja
