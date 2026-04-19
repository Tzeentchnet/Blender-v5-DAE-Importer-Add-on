# SPDX-License-Identifier: GPL-3.0-or-later
"""COLLADA (.dae) parsing and Blender mesh/armature builders.

Performance: hot paths use NumPy for parsing float / int streams and
``foreach_set`` for bulk mesh data transfer.
"""

import math
import os
import xml.etree.ElementTree as ET

import bpy
import numpy as np
from bpy_extras.io_utils import axis_conversion
from mathutils import Matrix, Vector


# ---------------------- XML / NAMESPACE HELPERS ----------------------

def get_collada_ns(root):
    """Return COLLADA namespace prefix '{...}' or empty."""
    if root.tag.startswith("{"):
        return root.tag.split("}")[0] + "}"
    return ""


def q(ns, tag):
    """Qualify XML tag with namespace."""
    return f"{ns}{tag}"


def _np_floats(text):
    """Parse a whitespace-separated float stream into an ndarray."""
    if not text:
        return np.empty(0, dtype=np.float64)
    return np.fromstring(text, dtype=np.float64, sep=" ")


def _np_ints(text):
    """Parse a whitespace-separated int stream into an ndarray."""
    if not text:
        return np.empty(0, dtype=np.int64)
    return np.fromstring(text, dtype=np.int64, sep=" ")


def parse_source_float_array(source_elem, ns):
    """Parse <source><float_array>; honor accessor stride.

    Returns an ``ndarray`` of shape ``(N, stride)`` (still indexable as
    ``arr[i]`` like the old list-of-tuples).
    """
    float_array = source_elem.find(q(ns, "float_array"))
    if float_array is None or float_array.text is None:
        return np.empty((0, 3), dtype=np.float64)
    floats = _np_floats(float_array.text)
    accessor = source_elem.find(f"{q(ns, 'technique_common')}/{q(ns, 'accessor')}")
    stride = int(accessor.attrib.get("stride", "3")) if accessor is not None else 3
    n = (floats.size // stride) * stride
    if n == 0:
        return np.empty((0, stride), dtype=np.float64)
    return floats[:n].reshape(-1, stride)


def parse_matrix(text):
    """Parse a 16-float COLLADA row-major matrix into a Blender Matrix."""
    vals = _np_floats(text)
    if vals.size != 16:
        return Matrix.Identity(4)
    return Matrix(vals.reshape(4, 4).tolist())


def get_up_axis_matrix(root, ns):
    """4x4 correction Matrix bringing the DAE space into Blender Z-up."""
    asset = root.find(q(ns, "asset"))
    up = asset.find(q(ns, "up_axis")) if asset is not None else None
    axis = up.text.strip().upper() if (up is not None and up.text) else "Y_UP"
    if axis == "Z_UP":
        return Matrix.Identity(4)
    if axis == "X_UP":
        return Matrix.Rotation(-math.pi / 2.0, 4, "Y")
    return Matrix.Rotation(math.pi / 2.0, 4, "X")  # Y_UP


def build_correction_matrix(root, ns, global_scale=1.0, forward_axis="-Y"):
    """Compose up-axis correction, forward-axis remap and uniform scale.

    The DAE's declared ``up_axis`` is corrected first (DAE -> Blender Z-up).
    Then ``forward_axis`` selects which post-correction axis maps to Blender's
    forward (-Y). Finally a uniform ``global_scale`` is applied.
    """
    up_correction = get_up_axis_matrix(root, ns)
    if forward_axis == "-Y":
        forward_correction = Matrix.Identity(4)
    else:
        forward_correction = axis_conversion(
            from_forward=forward_axis,
            from_up="Z",
            to_forward="-Y",
            to_up="Z",
        ).to_4x4()
    scale_mat = Matrix.Scale(global_scale, 4) if global_scale != 1.0 else Matrix.Identity(4)
    return scale_mat @ forward_correction @ up_correction


# ---------------------- MATERIAL / TEXTURE EXTRACTION ----------------------

def extract_material_texture_map(root, ns):
    """Return ``mat_id -> {channel: path}`` for all materials in the document."""
    image_path_for_id = {}
    for img in root.findall(f".//{q(ns, 'image')}"):
        img_id = img.attrib.get("id")
        if not img_id:
            continue
        init_from = img.find(q(ns, "init_from"))
        if init_from is not None and init_from.text:
            image_path_for_id[img_id] = init_from.text.strip()

    channels_for_effect = {}
    for eff in root.findall(f".//{q(ns, 'effect')}"):
        eff_id = eff.attrib.get("id")
        if not eff_id:
            continue

        sid_to_image = {}
        sid_to_surface = {}
        for newparam in eff.findall(f".//{q(ns, 'newparam')}"):
            sid = newparam.attrib.get("sid", "")
            surface = newparam.find(q(ns, "surface"))
            if surface is not None:
                inf = surface.find(q(ns, "init_from"))
                if inf is not None and inf.text:
                    sid_to_image[sid] = inf.text.strip()
            sampler = newparam.find(q(ns, "sampler2D"))
            if sampler is not None:
                src = sampler.find(q(ns, "source"))
                if src is not None and src.text:
                    sid_to_surface[sid] = src.text.strip()

        def resolve(tex_ref, s2surf=sid_to_surface, s2img=sid_to_image):
            if tex_ref in s2surf:
                image_id = s2img.get(s2surf[tex_ref], "")
            elif tex_ref in s2img:
                image_id = s2img[tex_ref]
            else:
                image_id = tex_ref
            return image_path_for_id.get(image_id)

        channels = {}
        shininess = 10.0
        spec_color = None

        profile = eff.find(q(ns, "profile_COMMON"))
        if profile is not None:
            technique = profile.find(q(ns, "technique"))
            if technique is not None:
                for shader in technique:
                    shader_tag = shader.tag.replace(ns, "")
                    if shader_tag not in ("phong", "lambert", "blinn", "constant"):
                        continue
                    for chan in shader:
                        chan_name = chan.tag.replace(ns, "")
                        tex = chan.find(q(ns, "texture"))
                        if tex is not None:
                            path = resolve(tex.attrib.get("texture", ""))
                            if path:
                                if chan_name == "diffuse":
                                    channels["diffuse"] = path
                                elif chan_name in ("bump", "normal"):
                                    channels["normal"] = path
                                elif chan_name == "transparent":
                                    channels["alpha"] = path
                                elif chan_name == "specular":
                                    channels["specular"] = path
                        # <color> fallback for diffuse/emission/transparent
                        # so DAEs without textures still get a sensible base
                        # color instead of default white-chrome.
                        if tex is None and chan_name in (
                            "diffuse", "emission", "transparent"
                        ):
                            cval = chan.find(q(ns, "color"))
                            if cval is not None and cval.text:
                                try:
                                    rgba = [float(x) for x in cval.text.strip().split()]
                                    while len(rgba) < 4:
                                        rgba.append(1.0)
                                    channels[f"_{chan_name}_color"] = tuple(rgba[:4])
                                except ValueError:
                                    pass
                        if chan_name == "shininess":
                            fval = chan.find(q(ns, "float"))
                            if fval is not None and fval.text:
                                try:
                                    shininess = float(fval.text.strip())
                                except ValueError:
                                    pass
                        if chan_name == "specular" and tex is None:
                            cval = chan.find(q(ns, "color"))
                            if cval is not None and cval.text:
                                try:
                                    rgba = [float(x) for x in cval.text.strip().split()]
                                    spec_color = rgba[:3]
                                except ValueError:
                                    pass

        roughness = max(0.2, min(0.95, 1.0 - (shininess / 128.0) ** 0.5))
        channels["_roughness"] = roughness
        channels["_spec_color"] = spec_color

        for tech in (
            eff.findall(f".//{q(ns, 'technique')}") + eff.findall(".//technique")
        ):
            profile_name = tech.attrib.get("profile", "")
            if profile_name in ("FCOLLADA", "OpenCOLLADA3dsMax", "MAX3D"):
                bump = tech.find("bump")
                if bump is not None:
                    tex = bump.find("texture")
                    if tex is not None:
                        path = resolve(tex.attrib.get("texture", ""))
                        if path:
                            channels.setdefault("normal", path)
                spec_lvl = tech.find("specularLevel")
                if spec_lvl is not None:
                    tex = spec_lvl.find("texture")
                    if tex is not None:
                        path = resolve(tex.attrib.get("texture", ""))
                        if path:
                            channels.setdefault("specular", path)

        all_tex_refs = [t.attrib.get("texture", "") for t in eff.findall(f".//{q(ns, 'texture')}")]
        all_paths = [p for p in (resolve(r) for r in all_tex_refs) if p]

        for path in all_paths:
            base = os.path.basename(path).lower()
            if any(h in base for h in ("_nrm", "_normal", "_norm", "normal_map", "_nor")):
                channels.setdefault("normal", path)
            elif any(h in base for h in ("_ao", "_ambient_occlusion", "_occlusion")):
                channels.setdefault("ao", path)
            elif any(h in base for h in ("_alb", "_albedo", "_diffuse", "_color", "_col", "_base")):
                channels.setdefault("diffuse", path)
            elif any(h in base for h in ("_spm", "_spec", "_specular", "_roughness", "_rgh")):
                channels.setdefault("specular", path)

        if "diffuse" not in channels and all_paths:
            channels["diffuse"] = all_paths[0]

        diff = channels.get("diffuse", "")
        diff_base = os.path.basename(diff).lower()
        non_albedo_hints = ("_ao", "_nrm", "_normal", "_spm", "_spec", "_bump")
        if any(h in diff_base for h in non_albedo_hints):
            for suffix in non_albedo_hints:
                if suffix in diff_base:
                    alb_name = diff_base.replace(suffix, "_alb")
                    alb_path = os.path.join(os.path.dirname(diff), alb_name)
                    if os.path.isfile(alb_path):
                        channels["diffuse"] = alb_path
                    break

        if channels:
            channels_for_effect[eff_id] = channels

    material_to_effect = {}
    for mat in root.findall(f".//{q(ns, 'material')}"):
        mat_id = mat.attrib.get("id")
        if not mat_id:
            continue
        inst = mat.find(f"./{q(ns, 'instance_effect')}")
        if inst is not None:
            eff_url = inst.attrib.get("url", "")[1:]
            material_to_effect[mat_id] = eff_url

    return {
        mat_id: channels_for_effect[eff_id]
        for mat_id, eff_id in material_to_effect.items()
        if eff_id in channels_for_effect
    }


# ---------------------- ARMATURE BUILDER ----------------------

def build_armature(root, ns, collection, model_name="Armature", correction_mat=None):
    """Build a Blender armature from joint hierarchy + INV_BIND matrices.

    Returns ``(armature_object, bsm_per_geom_dict)`` or ``(None, {})``.
    """
    vs = root.find(f".//{q(ns, 'visual_scene')}")
    if vs is None:
        return None, {}

    joint_bind_world = {}
    joint_bsm = {}

    ctrl_lib = root.find(f".//{q(ns, 'library_controllers')}")
    if ctrl_lib is not None:
        for ctrl in ctrl_lib.findall(q(ns, "controller")):
            skin = ctrl.find(q(ns, "skin"))
            if skin is None:
                continue
            geom_id = skin.attrib.get("source", "")[1:]

            bsm_elem = skin.find(q(ns, "bind_shape_matrix"))
            bsm = (
                parse_matrix(bsm_elem.text)
                if (bsm_elem is not None and bsm_elem.text)
                else Matrix.Identity(4)
            )
            joint_bsm[geom_id] = bsm

            joints_elem = skin.find(q(ns, "joints"))
            if joints_elem is None:
                continue
            jnames_src = ibm_src = None
            for inp in joints_elem.findall(q(ns, "input")):
                sem = inp.attrib.get("semantic", "")
                src = inp.attrib.get("source", "")[1:]
                if sem == "JOINT":
                    jnames_src = src
                elif sem == "INV_BIND_MATRIX":
                    ibm_src = src

            sources = {}
            for src in skin.findall(q(ns, "source")):
                sid = src.attrib.get("id", "")
                na = src.find(q(ns, "Name_array"))
                fa = src.find(q(ns, "float_array"))
                if na is not None and na.text:
                    sources[sid] = na.text.strip().split()
                elif fa is not None and fa.text:
                    sources[sid] = _np_floats(fa.text)

            jnames = sources.get(jnames_src, [])
            ibm_floats = sources.get(ibm_src, np.empty(0))
            ibm_floats = np.asarray(ibm_floats, dtype=np.float64)
            for i, jname in enumerate(jnames):
                if jname in joint_bind_world:
                    continue
                start = i * 16
                if start + 16 > ibm_floats.size:
                    continue
                inv_bind = Matrix(ibm_floats[start:start + 16].reshape(4, 4).tolist())
                try:
                    joint_bind_world[jname] = inv_bind.inverted()
                except Exception:
                    joint_bind_world[jname] = Matrix.Identity(4)

    if not joint_bind_world:
        return None, {}

    bone_info = {}

    def walk_joints(node, parent_id):
        node_id = node.attrib.get("id", "")
        node_name = node.attrib.get("name", node_id)
        node_type = node.attrib.get("type", "")
        if node_type == "JOINT" and node_id:
            bone_info[node_id] = {"name": node_name, "parent_id": parent_id}
            for child in node.findall(q(ns, "node")):
                walk_joints(child, node_id)
        else:
            for child in node.findall(q(ns, "node")):
                walk_joints(child, parent_id)

    for node in vs.findall(q(ns, "node")):
        walk_joints(node, None)

    arm_data = bpy.data.armatures.new(model_name)
    arm_data.display_type = "OCTAHEDRAL"
    arm_obj = bpy.data.objects.new(model_name, arm_data)
    collection.objects.link(arm_obj)

    bpy.context.view_layer.objects.active = arm_obj
    bpy.ops.object.mode_set(mode="EDIT")

    edit_bones = arm_data.edit_bones
    created = {}

    for bid, info in bone_info.items():
        if bid not in joint_bind_world:
            continue
        world = joint_bind_world[bid]
        head_world = world.to_translation()

        eb = edit_bones.new(info["name"])
        eb.head = head_world

        children_with_pos = [
            c for c, ci in bone_info.items()
            if ci["parent_id"] == bid and c in joint_bind_world
        ]
        if children_with_pos:
            child_heads = [joint_bind_world[c].to_translation() for c in children_with_pos]
            avg_child = sum(child_heads, Vector()) / len(child_heads)
            tail_vec = avg_child - head_world
            length = tail_vec.length
            eb.tail = (
                head_world + tail_vec.normalized() * max(length, 0.02)
                if length > 1e-4
                else head_world + Vector((0, 0, 0.05))
            )
        else:
            y_axis = world.to_3x3() @ Vector((0, 1, 0))
            y_axis = y_axis.normalized() if y_axis.length > 1e-6 else Vector((0, 0, 1))
            eb.tail = head_world + y_axis * 0.05

        if (eb.tail - eb.head).length < 1e-5:
            eb.tail = eb.head + Vector((0, 0, 0.05))

        created[bid] = eb

    for bid, info in bone_info.items():
        if bid not in created:
            continue
        pid = info["parent_id"]
        if pid and pid in created:
            created[bid].parent = created[pid]

    bpy.ops.object.mode_set(mode="OBJECT")
    print(f"Armature '{model_name}' created with {len(created)} bones.")
    return arm_obj, joint_bsm


# ---------------------- SKIN WEIGHT PARSER ----------------------

def parse_controllers(root, ns):
    """Return ``controller_id -> {skin_source, joint_names, vertex_weights, bind_shape_matrix}``."""
    result = {}
    ctrl_lib = root.find(f".//{q(ns, 'library_controllers')}")
    if ctrl_lib is None:
        return result

    for ctrl in ctrl_lib.findall(q(ns, "controller")):
        ctrl_id = ctrl.attrib.get("id", "")
        skin = ctrl.find(q(ns, "skin"))
        if skin is None:
            continue

        skin_source = skin.attrib.get("source", "")[1:]
        bsm_elem = skin.find(q(ns, "bind_shape_matrix"))
        bind_shape_matrix = (
            parse_matrix(bsm_elem.text)
            if (bsm_elem is not None and bsm_elem.text)
            else Matrix.Identity(4)
        )

        sources = {}
        for src in skin.findall(q(ns, "source")):
            src_id = src.attrib.get("id", "")
            name_arr = src.find(q(ns, "Name_array"))
            if name_arr is not None and name_arr.text:
                sources[src_id] = name_arr.text.strip().split()
                continue
            float_arr = src.find(q(ns, "float_array"))
            if float_arr is not None and float_arr.text:
                sources[src_id] = _np_floats(float_arr.text)

        joints_elem = skin.find(q(ns, "joints"))
        joint_names_src = None
        if joints_elem is not None:
            for inp in joints_elem.findall(q(ns, "input")):
                if inp.attrib.get("semantic") == "JOINT":
                    joint_names_src = inp.attrib.get("source", "")[1:]

        joint_names = sources.get(joint_names_src, []) if joint_names_src else []

        vw = skin.find(q(ns, "vertex_weights"))
        vertex_weights = {}
        if vw is not None:
            joint_offset = 0
            weight_offset = 1
            weight_src_id = None
            for inp in vw.findall(q(ns, "input")):
                sem = inp.attrib.get("semantic", "")
                off = int(inp.attrib.get("offset", "0"))
                src = inp.attrib.get("source", "")[1:]
                if sem == "JOINT":
                    joint_offset = off
                elif sem == "WEIGHT":
                    weight_offset = off
                    weight_src_id = src

            weight_values = (
                np.asarray(sources.get(weight_src_id, []), dtype=np.float64)
                if weight_src_id else np.empty(0)
            )
            vcount_elem = vw.find(q(ns, "vcount"))
            v_elem = vw.find(q(ns, "v"))

            if vcount_elem is not None and v_elem is not None and vcount_elem.text and v_elem.text:
                vcounts = _np_ints(vcount_elem.text)
                v_data = _np_ints(v_elem.text)
                num_inputs = max(joint_offset, weight_offset) + 1

                # Reshape index stream for vectorized lookup
                pairs_total = v_data.size // num_inputs
                v_view = v_data[: pairs_total * num_inputs].reshape(-1, num_inputs)
                joint_idx_all = v_view[:, joint_offset]
                weight_idx_all = v_view[:, weight_offset]

                # Cumulative offsets per vertex
                ends = np.cumsum(vcounts)
                starts = np.concatenate(([0], ends[:-1]))

                wv_size = weight_values.size
                for vert_idx in range(vcounts.size):
                    s, e = int(starts[vert_idx]), int(ends[vert_idx])
                    if s == e:
                        continue
                    j_slice = joint_idx_all[s:e]
                    w_slice = weight_idx_all[s:e]
                    valid = (w_slice >= 0) & (w_slice < wv_size)
                    if not valid.any():
                        continue
                    wvals = weight_values[w_slice[valid]]
                    pairs = list(zip(j_slice[valid].tolist(), wvals.tolist()))
                    vertex_weights[vert_idx] = pairs

        result[ctrl_id] = {
            "skin_source": skin_source,
            "joint_names": joint_names,
            "vertex_weights": vertex_weights,
            "bind_shape_matrix": bind_shape_matrix,
        }

    return result


def build_ctrl_mat_map(root, ns, controllers):
    """Return ``geometry_id -> {material_symbol: material_target_id}``."""
    geom_to_mat_override = {}
    for ic in root.findall(f".//{q(ns, 'instance_controller')}"):
        ctrl_url = ic.attrib.get("url", "")[1:]
        if ctrl_url not in controllers:
            continue
        geom_id = controllers[ctrl_url]["skin_source"]
        mat_map = {}
        for im in ic.findall(f".//{q(ns, 'instance_material')}"):
            symbol = im.attrib.get("symbol", "")
            target = im.attrib.get("target", "")[1:]
            mat_map[symbol] = target
        geom_to_mat_override[geom_id] = mat_map
    return geom_to_mat_override


# ---------------------- GEOMETRY IMPORTER ----------------------

def build_mesh_from_geometry(
    geom_elem, ns, collection, material_texture_map,
    arm_obj, controllers, ctrl_mat_override, dae_filepath,
    armature_node_mat=None,
    source_cache=None,
):
    """Convert <geometry> -> Blender mesh (positions, normals, colors, UVs,
    materials, textures, optional skin weights linked to ``arm_obj``)."""
    mesh_elem = geom_elem.find(q(ns, "mesh"))
    if mesh_elem is None:
        print("Skipping geometry (no <mesh>):", geom_elem.attrib.get("id"))
        return None

    geom_id = geom_elem.attrib.get("id", "")
    geom_name = geom_elem.attrib.get("name") or geom_id or "DAE_Mesh"

    if source_cache is None:
        source_cache = {}

    sources = {}
    for src in mesh_elem.findall(q(ns, "source")):
        src_id = src.attrib.get("id")
        if not src_id:
            continue
        cached = source_cache.get(src_id)
        if cached is None:
            cached = parse_source_float_array(src, ns)
            source_cache[src_id] = cached
        sources[src_id] = cached

    vertices_map = {}
    for verts in mesh_elem.findall(q(ns, "vertices")):
        v_id = verts.attrib.get("id")
        if not v_id:
            continue
        for inp in verts.findall(q(ns, "input")):
            if inp.attrib.get("semantic") == "POSITION":
                src_val = inp.attrib.get("source", "")
                vertices_map[v_id] = src_val[1:] if src_val.startswith("#") else src_val

    positions = None
    faces = []
    face_mat_ids = []
    corner_uvs = []
    corner_cols = []
    corner_norms = []

    prim_blocks = (
        [(tri, None) for tri in mesh_elem.findall(q(ns, "triangles"))]
        + [(pl, pl.find(q(ns, "vcount"))) for pl in mesh_elem.findall(q(ns, "polylist"))]
    )

    for prim, vcount_elem in prim_blocks:
        count = int(prim.attrib.get("count", "0"))
        p_elem = prim.find(q(ns, "p"))
        if p_elem is None or not p_elem.text:
            continue

        tri_mat_symbol = prim.attrib.get("material")
        tri_mat_id = ctrl_mat_override.get(tri_mat_symbol, tri_mat_symbol)

        input_by_offset = {}
        max_offset = 0
        for inp in prim.findall(q(ns, "input")):
            sem = inp.attrib.get("semantic")
            src_val = inp.attrib.get("source", "")
            src = src_val[1:] if src_val.startswith("#") else src_val
            off = int(inp.attrib.get("offset", "0"))
            set_i = inp.attrib.get("set")
            input_by_offset[off] = (sem, src, set_i)
            max_offset = max(max_offset, off)

        num_inputs = max_offset + 1

        vertex_offset = 0
        pos_source_id = None
        for off, (sem, src, _) in input_by_offset.items():
            if sem == "VERTEX":
                vertex_offset = off
                pos_source_id = vertices_map.get(src)
                if pos_source_id:
                    break

        if pos_source_id is None:
            for off, (sem, src, _) in input_by_offset.items():
                if sem == "POSITION":
                    vertex_offset = off
                    pos_source_id = src
                    break

        if pos_source_id is None:
            for src_id in sources.keys():
                if "position" in src_id.lower():
                    pos_source_id = src_id
                    for off, (_s_sem, s_src, _) in input_by_offset.items():
                        if s_src == pos_source_id:
                            vertex_offset = off
                            break
                    break

        if pos_source_id is None:
            print(f"DEBUG: Failed to find POSITION for {geom_name}")
            for sid in sources.keys():
                if "pos" in sid.lower():
                    pos_source_id = sid
                    for off, (_s_sem, s_src, _) in input_by_offset.items():
                        if s_src == pos_source_id:
                            vertex_offset = off
                            break
                    break
            if pos_source_id is None:
                return None

        positions = sources.get(pos_source_id)
        if positions is None or len(positions) == 0:
            print("Position source missing:", pos_source_id)
            return None

        normal_offset = uv_offset = color_offset = None
        normal_source = uv_source = color_source = None
        for off, (sem, src, set_idx) in input_by_offset.items():
            if sem == "NORMAL":
                normal_offset = off
                normal_source = sources.get(src)
            elif sem == "COLOR":
                color_offset = off
                color_source = sources.get(src)
            elif sem == "TEXCOORD":
                if uv_source is None or set_idx == "0":
                    uv_offset = off
                    uv_source = sources.get(src)

        # NumPy-fast index stream parse + reshape
        raw_idx_arr = _np_ints(p_elem.text)

        if vcount_elem is not None and vcount_elem.text:
            vcount_arr = _np_ints(vcount_elem.text)
        else:
            vcount_arr = np.full(count, 3, dtype=np.int64)

        if vcount_arr.size == 0:
            continue

        # Reshape full index stream for vectorized column extraction
        total_corners = int(vcount_arr.sum())
        usable = total_corners * num_inputs
        idx_view = (
            raw_idx_arr[:usable].reshape(-1, num_inputs)
            if usable
            else np.empty((0, num_inputs), dtype=np.int64)
        )

        # ---- Vectorized fan triangulation ----
        # For each polygon of n corners, emit triangles (0, i, i+1) for i in 1..n-2,
        # using corner-stream offsets (not vertex indices) so we can gather any attribute.
        n_tris_per_poly = np.maximum(vcount_arr - 2, 0)
        T = int(n_tris_per_poly.sum())
        if T == 0:
            continue

        poly_starts = np.concatenate(
            ([0], np.cumsum(vcount_arr)[:-1])
        ).astype(np.int64)

        # Per-tri starting corner-offset (corner index of fan vertex 0)
        tri_start = np.repeat(poly_starts, n_tris_per_poly)
        # Within-poly j offset for each tri (1..n-2)
        tri_j = np.concatenate([
            np.arange(1, int(v) - 1, dtype=np.int64)
            for v in vcount_arr if v >= 3
        ])

        c0 = tri_start
        c1 = tri_start + tri_j
        c2 = tri_start + tri_j + 1

        # Position indices per triangle (vertex indices into the position source)
        pos_col = idx_view[:, vertex_offset]
        tri_v = np.stack([pos_col[c0], pos_col[c1], pos_col[c2]], axis=1)

        # Drop degenerate triangles (any two vertices coincident)
        keep = (
            (tri_v[:, 0] != tri_v[:, 1])
            & (tri_v[:, 1] != tri_v[:, 2])
            & (tri_v[:, 0] != tri_v[:, 2])
        )
        tri_v = tri_v[keep]
        n_kept = int(tri_v.shape[0])
        if n_kept == 0:
            continue

        faces.extend(tri_v.tolist())
        face_mat_ids.extend([tri_mat_id] * n_kept)

        def _gather(off, source_arr, default_row, dim):
            """Gather (n_kept*3, dim) corner attribute via fan corner indices."""
            arr = np.asarray(source_arr, dtype=np.float64)
            if arr.ndim == 1:
                arr = arr.reshape(-1, dim)
            if arr.shape[1] < dim:
                pad = np.tile(np.asarray(default_row[arr.shape[1]:], dtype=np.float64),
                              (arr.shape[0], 1))
                arr = np.concatenate([arr, pad], axis=1)
            elif arr.shape[1] > dim:
                arr = arr[:, :dim]
            n = arr.shape[0]
            cols = idx_view[:, off]
            if n == 0:
                return np.tile(np.asarray(default_row, dtype=np.float64),
                               (n_kept * 3, 1))
            safe = np.clip(cols, 0, n - 1)
            stacked = np.stack(
                [arr[safe[c0]], arr[safe[c1]], arr[safe[c2]]], axis=1
            )  # (T, 3, dim)
            return stacked[keep].reshape(-1, dim)

        if normal_offset is not None and normal_source is not None:
            corner_norms.extend(
                _gather(normal_offset, normal_source, (0.0, 0.0, 1.0), 3).tolist()
            )

        if color_offset is not None and color_source is not None:
            color_dim = (
                4 if (hasattr(color_source, "shape") and color_source.shape[1:] == (4,))
                else 4  # always emit 4-component; _gather pads with defaults
            )
            corner_cols.extend(
                _gather(color_offset, color_source, (1.0, 1.0, 1.0, 1.0), color_dim).tolist()
            )

        if uv_offset is not None and uv_source is not None:
            corner_uvs.extend(
                _gather(uv_offset, uv_source, (0.0, 0.0), 2).tolist()
            )

    if positions is None or len(positions) == 0 or not faces:
        print("No valid geometry in:", geom_name)
        return None

    # ---------------------- CREATE MESH (NumPy + foreach_set) ----------------------
    # Apply bind_shape_matrix to mesh-space positions
    skin_ctrl = next((c for c in controllers.values() if c["skin_source"] == geom_id), None)
    pos_arr = np.asarray(positions, dtype=np.float64)  # (V, 3)
    if skin_ctrl is not None:
        bsm = skin_ctrl.get("bind_shape_matrix", Matrix.Identity(4))
        if bsm != Matrix.Identity(4):
            bsm_np = np.array([list(r) for r in bsm], dtype=np.float64)  # (4,4)
            ones = np.ones((pos_arr.shape[0], 1), dtype=np.float64)
            homo = np.concatenate([pos_arr, ones], axis=1)  # (V,4)
            pos_arr = (homo @ bsm_np.T)[:, :3]

    mesh = bpy.data.meshes.new(geom_name)

    faces_arr = np.asarray(faces, dtype=np.int32)  # (F, 3)
    n_verts = pos_arr.shape[0]
    n_faces = faces_arr.shape[0]
    n_loops = n_faces * 3

    mesh.vertices.add(n_verts)
    mesh.vertices.foreach_set("co", pos_arr.astype(np.float32).ravel())

    mesh.loops.add(n_loops)
    mesh.loops.foreach_set("vertex_index", faces_arr.ravel())

    mesh.polygons.add(n_faces)
    loop_starts = (np.arange(n_faces, dtype=np.int32) * 3)
    mesh.polygons.foreach_set("loop_start", loop_starts)
    # `loop_total` is read-only on Blender >=4.1; ignore the failure if so.
    try:
        mesh.polygons.foreach_set("loop_total", np.full(n_faces, 3, dtype=np.int32))
    except (RuntimeError, AttributeError):
        pass

    mesh.update(calc_edges=True)
    mesh.validate(verbose=False)

    obj = bpy.data.objects.new(geom_name, mesh)
    collection.objects.link(obj)

    # ---------------------- MATERIALS ----------------------
    dae_dir = os.path.dirname(bpy.path.abspath(dae_filepath))

    def _resolve_tex(raw_path):
        if not raw_path:
            return None
        for candidate in [
            raw_path,
            os.path.join(dae_dir, raw_path),
            os.path.join(dae_dir, os.path.basename(raw_path)),
        ]:
            candidate = os.path.normpath(candidate)
            if os.path.isfile(candidate):
                return candidate
        return None

    def _load_img(raw_path, colorspace="sRGB"):
        resolved = _resolve_tex(raw_path)
        if not resolved:
            return None
        try:
            img = bpy.data.images.load(resolved, check_existing=True)
            img.colorspace_settings.name = colorspace
            return img
        except Exception as e:
            print(f"Failed to load texture '{resolved}': {e}")
            return None

    def _mat_diffuse_path(m):
        if not m.use_nodes:
            return None
        for n in m.node_tree.nodes:
            if n.type == "TEX_IMAGE" and n.image and n.label == "diffuse":
                return os.path.normpath(bpy.path.abspath(n.image.filepath))
        return None

    def _build_mat_nodes(m, channels, has_second_uv=False):
        m.use_nodes = True
        nodes = m.node_tree.nodes
        links = m.node_tree.links
        nodes.clear()

        out_n = nodes.new("ShaderNodeOutputMaterial")
        out_n.location = (700, 0)
        bsdf_n = nodes.new("ShaderNodeBsdfPrincipled")
        bsdf_n.location = (300, 0)
        links.new(bsdf_n.outputs["BSDF"], out_n.inputs["Surface"])

        roughness = channels.get("_roughness", 0.8)
        bsdf_n.inputs["Roughness"].default_value = roughness

        spec_color = channels.get("_spec_color")
        if spec_color is not None:
            spec_intensity = (spec_color[0] + spec_color[1] + spec_color[2]) / 3.0
        else:
            spec_intensity = 0.05
        for inp_name in ("Specular IOR Level", "Specular"):
            if inp_name in bsdf_n.inputs:
                bsdf_n.inputs[inp_name].default_value = min(1.0, spec_intensity)
                break

        x = -400

        diff_path = channels.get("diffuse")
        if diff_path:
            img = _load_img(diff_path, "sRGB")
            if img:
                n = nodes.new("ShaderNodeTexImage")
                n.image = img
                n.label = "diffuse"
                n.location = (x, 200)
                links.new(n.outputs["Color"], bsdf_n.inputs["Base Color"])
                links.new(n.outputs["Alpha"], bsdf_n.inputs["Alpha"])
                m.blend_method = "CLIP"
        else:
            # No diffuse texture: fall back to <diffuse><color> if present,
            # otherwise a neutral mid-gray so the surface isn't rendered as
            # default white (which reads as chrome under the studio matcap).
            diff_color = channels.get("_diffuse_color")
            if diff_color is not None:
                bsdf_n.inputs["Base Color"].default_value = diff_color
                if "Alpha" in bsdf_n.inputs and diff_color[3] < 1.0:
                    bsdf_n.inputs["Alpha"].default_value = diff_color[3]
                    m.blend_method = "BLEND"
            else:
                bsdf_n.inputs["Base Color"].default_value = (0.5, 0.5, 0.5, 1.0)

        nrm_path = channels.get("normal")
        if nrm_path:
            img = _load_img(nrm_path, "Non-Color")
            if img:
                img_n = nodes.new("ShaderNodeTexImage")
                img_n.location = (x - 300, -200)
                img_n.image = img
                img_n.label = "normal"
                if has_second_uv:
                    nrm_n = nodes.new("ShaderNodeNormalMap")
                    nrm_n.location = (x, -200)
                    links.new(img_n.outputs["Color"], nrm_n.inputs["Color"])
                    links.new(nrm_n.outputs["Normal"], bsdf_n.inputs["Normal"])

        ao_path = channels.get("ao")
        if ao_path and diff_path:
            img = _load_img(ao_path, "Non-Color")
            if img:
                ao_n = nodes.new("ShaderNodeTexImage")
                ao_n.location = (x - 300, 450)
                mix_n = nodes.new("ShaderNodeMixRGB")
                mix_n.location = (x, 450)
                ao_n.image = img
                ao_n.label = "ao"
                mix_n.blend_type = "MULTIPLY"
                mix_n.inputs[0].default_value = 1.0
                diff_node = next(
                    (n for n in nodes if n.type == "TEX_IMAGE" and n.label == "diffuse"),
                    None,
                )
                if diff_node:
                    links.new(diff_node.outputs["Color"], mix_n.inputs[1])
                    links.new(ao_n.outputs["Color"], mix_n.inputs[2])
                    for lnk in list(links):
                        if lnk.to_socket == bsdf_n.inputs["Base Color"]:
                            links.remove(lnk)
                    links.new(mix_n.outputs["Color"], bsdf_n.inputs["Base Color"])

        spec_path = channels.get("specular")
        if spec_path:
            img = _load_img(spec_path, "Non-Color")
            if img:
                n = nodes.new("ShaderNodeTexImage")
                n.location = (x, -450)
                n.image = img
                n.label = "specular"

    has_second_uv = any(
        inp.attrib.get("semantic") == "TEXCOORD" and inp.attrib.get("set", "0") == "1"
        for prim in mesh_elem
        for inp in prim.findall(q(ns, "input"))
    )

    unique_mat_ids = sorted({m for m in face_mat_ids if m is not None})
    mat_index_map = {}
    obj.data.materials.clear()

    for idx, mat_id in enumerate(unique_mat_ids):
        channels = material_texture_map.get(mat_id, {})
        diff_path = _resolve_tex(channels.get("diffuse"))
        tex_base = (
            os.path.splitext(os.path.basename(diff_path))[0] if diff_path else mat_id
        )

        existing = bpy.data.materials.get(tex_base)
        want_path = os.path.normpath(diff_path) if diff_path else None
        if existing is not None and _mat_diffuse_path(existing) == want_path:
            mat = existing
        else:
            mat = bpy.data.materials.new(tex_base)
            _build_mat_nodes(mat, dict(channels), has_second_uv)
            print(
                f"Material built: '{mat.name}' "
                f"(diffuse={os.path.basename(diff_path) if diff_path else 'none'})"
            )

        obj.data.materials.append(mat)
        mat_index_map[mat_id] = idx

    if face_mat_ids:
        mat_indices = np.array(
            [mat_index_map.get(m, 0) for m in face_mat_ids], dtype=np.int32
        )
        mesh.polygons.foreach_set("material_index", mat_indices)

    # ---------------------- UVs ----------------------
    if corner_uvs and len(corner_uvs) == len(mesh.loops):
        uv_layer = mesh.uv_layers.new(name="UVMap")
        uv_flat = np.array(corner_uvs, dtype=np.float32).ravel()
        uv_layer.data.foreach_set("uv", uv_flat)

    # ---------------------- COLORS ----------------------
    if corner_cols and len(corner_cols) == len(mesh.loops):
        col_attr = mesh.color_attributes.new(name="Col", type="FLOAT_COLOR", domain="CORNER")
        col_flat = np.array(corner_cols, dtype=np.float32).ravel()
        col_attr.data.foreach_set("color", col_flat)

    # ---------------------- NORMALS ----------------------
    if corner_norms and len(corner_norms) == len(mesh.loops):
        # Custom split normals only take effect on smooth-shaded polygons in
        # Blender 4.1+. New mesh polygons default to flat (use_smooth=False),
        # which causes the face normal to override the custom corner normals
        # and produces faceted/chrome-looking shading. Mark all polygons smooth
        # before assigning split normals so the DAE-supplied normals are used.
        mesh.polygons.foreach_set(
            "use_smooth", np.ones(n_faces, dtype=bool)
        )
        mesh.normals_split_custom_set(corner_norms)

    # ---------------------- SKIN WEIGHTS ----------------------
    if arm_obj is not None and skin_ctrl is not None:
        joint_names = skin_ctrl["joint_names"]
        vertex_weights = skin_ctrl["vertex_weights"]

        vgroups = {jname: obj.vertex_groups.new(name=jname) for jname in joint_names}

        # Group (vert, weight) by bone index so we can issue one bulk add per bone.
        # Also sums duplicate joint refs per vertex (matches DAE intent better than per-pair ADD).
        per_bone_vert_weight = {}
        for vert_idx, pairs in vertex_weights.items():
            summed = {}
            for j_idx, weight in pairs:
                if j_idx < 0 or j_idx >= len(joint_names) or weight <= 0.0:
                    continue
                summed[j_idx] = summed.get(j_idx, 0.0) + weight
            for j_idx, weight in summed.items():
                per_bone_vert_weight.setdefault(j_idx, {}).setdefault(weight, []).append(vert_idx)

        for j_idx, weight_buckets in per_bone_vert_weight.items():
            jname = joint_names[j_idx]
            vg = vgroups[jname]
            for weight, vert_list in weight_buckets.items():
                vg.add(vert_list, float(weight), "REPLACE")

        obj.parent = arm_obj
        mod = obj.modifiers.new(name="Armature", type="ARMATURE")
        mod.object = arm_obj
        mod.use_vertex_groups = True

        print(f"Skin weights applied to '{geom_name}' ({len(vgroups)} bone groups).")

    return obj


# ---------------------- TOP-LEVEL IMPORT ----------------------

def parse_node_transform(node, ns):
    """Compose a 4x4 Matrix from a node's transform children."""
    combined = Matrix.Identity(4)
    for child in node:
        tag = child.tag.replace(ns, "")
        if tag == "matrix" and child.text:
            combined @= parse_matrix(child.text)
        elif tag == "translate" and child.text:
            v = [float(x) for x in child.text.split()]
            combined @= Matrix.Translation(Vector(v))
        elif tag == "rotate" and child.text:
            vals = [float(x) for x in child.text.split()]
            if len(vals) == 4:
                axis = Vector(vals[:3])
                angle = vals[3]
                combined @= Matrix.Rotation(math.radians(angle), 4, axis)
        elif tag == "scale" and child.text:
            s = [float(x) for x in child.text.split()]
            combined @= Matrix.Diagonal(Vector((s[0], s[1], s[2], 1.0)))
    return combined


def import_dae(
    filepath,
    context,
    import_rig=True,
    global_scale=1.0,
    forward_axis="-Y",
    wm=None,
):
    """Import a single .dae file into the active collection.

    Returns ``(imported_count, armature_object_or_None, error_msg_or_None)``.
    """
    if not os.path.isfile(filepath):
        return 0, None, f"File not found: {filepath}"

    try:
        tree = ET.parse(filepath)
        root = tree.getroot()
    except Exception as e:
        return 0, None, f"Failed to parse DAE: {e}"

    ns = get_collada_ns(root)

    if context.view_layer.active_layer_collection:
        collection = context.view_layer.active_layer_collection.collection
    else:
        collection = context.scene.collection

    material_texture_map = extract_material_texture_map(root, ns)
    model_name = os.path.splitext(os.path.basename(filepath))[0]
    correction_mat = build_correction_matrix(
        root, ns, global_scale=global_scale, forward_axis=forward_axis
    )

    arm_obj = None
    armature_node_mat = Matrix.Identity(4)
    controllers = {}
    if import_rig:
        arm_obj, armature_node_mat = build_armature(
            root, ns, collection, model_name, correction_mat
        )
        controllers = parse_controllers(root, ns)

    geom_mat_override = build_ctrl_mat_map(root, ns, controllers)

    geometries = root.findall(f".//{q(ns, 'geometry')}")
    if not geometries:
        return 0, arm_obj, "No <geometry> found in DAE"

    vs = root.find(f".//{q(ns, 'visual_scene')}")
    if vs is None:
        return 0, arm_obj, "No <visual_scene> found in DAE"

    geom_map = {g.attrib.get("id"): g for g in geometries}
    imported = 0
    geom_total = max(len(geom_map), 1)
    source_cache = {}

    def walk_scene(node, parent_mat):
        nonlocal imported
        local_mat = parse_node_transform(node, ns)
        world_mat = parent_mat @ local_mat

        for ig in node.findall(q(ns, "instance_geometry")):
            geom_url = ig.attrib.get("url", "")[1:]
            if geom_url in geom_map:
                geom = geom_map[geom_url]
                mat_override = geom_mat_override.get(geom_url, {})
                obj = build_mesh_from_geometry(
                    geom, ns, collection, material_texture_map,
                    arm_obj, controllers, mat_override, filepath,
                    armature_node_mat,
                    source_cache=source_cache,
                )
                if obj:
                    obj.matrix_world = world_mat
                    imported += 1
                    if wm is not None:
                        try:
                            wm.progress_update(imported / geom_total)
                        except Exception:
                            pass

        for child in node.findall(q(ns, "node")):
            walk_scene(child, world_mat)

    for node in vs.findall(q(ns, "node")):
        walk_scene(node, correction_mat)

    return imported, arm_obj, None
