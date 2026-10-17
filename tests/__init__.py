import os
import sys

# Plugin modules are imported as top-level modules, like Nicotine+ does
PLUGIN_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "nicotine_mcp")

if PLUGIN_PATH not in sys.path:
    sys.path.insert(0, PLUGIN_PATH)
