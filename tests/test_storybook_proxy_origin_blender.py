import json
from pathlib import Path
import shutil
import subprocess

import pytest


def test_proxy_support_origin_survives_instance_location_assignment():
    blender = shutil.which('blender')
    if not blender:
        pytest.skip('Blender is not installed')
    root = Path(__file__).resolve().parents[1]
    script = f'''
import sys, json, bpy
sys.path.insert(0, {str(root)!r})
from mathutils import Vector
from custom_tools.storybook.blockout_blender.build_scene import build_proxy_object
results = []
for kind in ('biped', 'quadruped', 'none'):
    obj = build_proxy_object(kind, kind, 1.7)
    origin = list(obj.location)
    obj.location = (0, 0, 0)
    bpy.context.view_layer.update()
    corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    results.append(dict(kind=kind, origin=origin, bottom=min(c.z for c in corners)))
print('PROXY_ORIGINS=' + json.dumps(results))
'''
    result = subprocess.run([blender, '-b', '--factory-startup', '--python-expr', script],
                            capture_output=True, text=True, check=True, timeout=60)
    line = next(line for line in result.stdout.splitlines() if line.startswith('PROXY_ORIGINS='))
    for proxy in json.loads(line.split('=', 1)[1]):
        assert proxy['origin'] == pytest.approx([0, 0, 0], abs=1e-5), proxy['kind']
        assert proxy['bottom'] == pytest.approx(0, abs=1e-5), proxy['kind']
