import os
import logging

import numpy as np

from flask import Flask, Response, request, abort

from neuclease import configure_default_logging
from neuclease.util import Timer
from neuclease.dvid import (default_dvid_session, find_master, fetch_volume_box, fetch_labelmap_voxels,
                            fetch_sparsevol_coarse, fetch_sparsevol, post_key)
from neuclease.dvid.rle import rle_ranges_box

from vol2mesh import Mesh

app = Flask(__name__)

logger = logging.getLogger(__name__)
configure_default_logging()

MB = 2**20
MAX_BOX_VOLUME = 128*MB

# FIXME: For now, this resolution is hard-coded.
VOXEL_NM = 8.0

# FIXME: This shouldn't be hard-coded, either
MAX_SCALE = 7

# TODO: Should this function check DVID to see if a mesh already exists
#       for the requested body, or should we assume the caller doesn't
#       want that one?

@app.route('/')
def generate_and_store_mesh():
    try:
        body = request.args['body']
    except KeyError as ex:
        abort(Response(f"Missing required parameter: {ex.args[0]}", 400))

    with Timer(f"Body {body}: Handling request", logger):
        return _generate_and_store_mesh()


def _generate_and_store_mesh():
    try:
        dvid = request.args['dvid']
        body = request.args['body']
    except KeyError as ex:
        abort(Response(f"Missing required parameter: {ex.args[0]}", 400))

    segmentation = request.args.get('segmentation', 'segmentation')
    mesh_kv = request.args.get('mesh_kv', f'{segmentation}_meshes')

    uuid = request.args.get('uuid') or find_master(dvid)
    if not uuid:
        uuid = find_master(dvid)

    scale = request.args.get('scale')
    smoothing = request.args.get('smoothing', 2)

    # Note: This is just the effective desired decimation assuming scale-1 data.
    # If we're forced to select a higher scale than scale-1, then we'll increase
    # this number to compensate.
    decimation = request.args.get('decimation', 0.1)

    user = request.args.get('u')
    user = user or request.args.get('user', "UNKNOWN")

    dvid_session = default_dvid_session('cloud-meshgen', user)
    auth = request.headers.get('Authorization')
    if auth:
        dvid_session.headers['Authorization'] = auth

    with Timer(f"Body {body}: Fetching coarse sparsevol"):
        svc_ranges = fetch_sparsevol_coarse(dvid, uuid, segmentation, body, format='ranges', session=dvid_session)

    #svc_mask, _svc_box = fetch_sparsevol_coarse(dvid, uuid, segmentation, body, format='mask', session=dvid_session)
    #np.save(f'mask-{body}-svc.npy', svc_mask)

    box_s6 = rle_ranges_box(svc_ranges)
    box_s0 = box_s6*(2**6)
    logger.info(f"Body {body}: Bounding box: {box_s0[:, ::-1].tolist()}")

    if scale is None:
        # Use scale 1 if possible or a higher scale
        # if necessary due to bounding-box RAM usage.
        scale = max(1, select_scale(box_s0))

    if scale > 1:
        # If we chose a low-res scale, then we
        # can reduce the decimation as needed.
        decimation = min(1.0, decimation * 4**(scale-1))

    with Timer(f"Body {body}: Fetching scale-{scale} sparsevol"):
        mask, mask_box = fetch_sparsevol(dvid, uuid, segmentation, body, scale=scale, format='mask', session=dvid_session)
        #np.save(f'mask-{body}-s{scale}.npy', mask)

        # Pad with a thin halo of zeros to avoid holes in the mesh at the box boundary
        mask = np.pad(mask, 1)
        mask_box += [(-1, -1, -1), (1, 1, 1)]

    with Timer(f"Body {body}: Computing mesh"):
        # The 'ilastik' marching cubes implementation supports smoothing during mesh construction.
        mesh = Mesh.from_binary_vol(mask, mask_box * VOXEL_NM * (2**scale), smoothing_rounds=smoothing)

        logger.info(f"Body {body}: Decimating mesh at fraction {decimation}")
        mesh.simplify(decimation)

        logger.info(f"Body {body}: Preparing ngmesh")
        mesh_bytes = mesh.serialize(fmt='ngmesh')

    with Timer(f"Body {body}: Storing {body}.ngmesh in DVID ({len(mesh_bytes)/MB:.1f} MB)"):
        post_key(dvid, uuid, mesh_kv, f"{body}.ngmesh", mesh_bytes, session=dvid_session)

    return mesh_bytes


def select_scale(box):
    scale = 0
    box = np.array(box)
    while np.prod(box[1] - box[0]) > MAX_BOX_VOLUME:
        scale += 1
        box //= 2

    if scale > MAX_SCALE:
        abort(Response(
                "Can't generate mesh for body {body}: "
                "The bounding box would be too large, even at scale {MAX_SCALE}",
                500))

    return scale


if __name__ == "__main__":
    app.run(debug=True,host='0.0.0.0',port=int(os.environ.get('PORT', 8080)))