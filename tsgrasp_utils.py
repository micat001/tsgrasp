"""
Collection of utility functions for grasp_synthesis
Specific to tsgrasp as it imports Minkowski engine

Author: Tim Player
"""
# Standard library
from dataclasses import dataclass
from typing import List

# Third-party
import MinkowskiEngine
import numpy as np
import torch
import yaml
from matplotlib import colormaps as cm


@dataclass
class PyPose:
    """
    Dataclass version of a ROS geometry_msgs/Pose
    """

    position: list
    orientation: list


@dataclass
class PyGrasps:
    """
    Dataclass version of a raven_manip_msgs Grasps
    """

    poses: List[PyPose]
    orbital_poses: List[PyPose]
    confs: List[float]
    widths: List[float]


def quaternion_to_rotation_matrix(quat: list) -> np.array:
    """
    Converts quaternion to 3x3 rotation matrix.
    No extra dependencies.

    Args:
        quat (list): Input quat as list

    Returns:
        np.array: output 3x3 matrix
    """
    # Extract the values from Q
    q0 = quat[0]
    q1 = quat[1]
    q2 = quat[2]
    q3 = quat[3]

    # First row of the rotation matrix
    r00 = 2 * (q0 * q0 + q1 * q1) - 1
    r01 = 2 * (q1 * q2 - q0 * q3)
    r02 = 2 * (q1 * q3 + q0 * q2)

    # Second row of the rotation matrix
    r10 = 2 * (q1 * q2 + q0 * q3)
    r11 = 2 * (q0 * q0 + q2 * q2) - 1
    r12 = 2 * (q2 * q3 - q0 * q1)

    # Third row of the rotation matrix
    r20 = 2 * (q1 * q3 - q0 * q2)
    r21 = 2 * (q2 * q3 + q0 * q1)
    r22 = 2 * (q0 * q0 + q3 * q3) - 1

    # 3x3 rotation matrix
    rot_matrix = np.array([[r00, r01, r02], [r10, r11, r12], [r20, r21, r22]])

    return rot_matrix


def generate_color_lookup(cm_str="magma") -> np.array:
    """
    Generates integer lookup array for ints -> colors

    Args:
        cm_str (str, optional): Colormap string. Defaults to "magma".

    Returns:
        np.array: colormap lookup array
    """
    color_lookup = np.zeros(shape=(256, 4))
    colormap = cm[cm_str]
    for aa in range(256):
        red, green, blue, _ = colormap(aa)
        color_lookup[aa, :] = [red, green, blue, 255]

    return color_lookup


def model_metadata_from_yaml(yaml_file_path: str) -> dict:
    """
    Gets config dictionary from yaml file

    Args:
        yaml_file_path (str): path (relative) to yaml config file

    Returns:
        metadata (dict): metadata dictionary
    """
    metadata = {}

    try:
        with open(yaml_file_path, "r", encoding="utf-8") as stream:
            metadata = yaml.safe_load(stream)
    except yaml.YAMLError as ex:
        print(ex)
    return metadata


# @torch.jit.script
def eul_to_rotm(roll: float, pitch: float, yaw: float) -> np.array:
    """
    Convert euler angles to rotation matrix."
    From: https://stackoverflow.com/questions/59387182/construct-a-rotation-matrix-in-pytorch
    Args:
        roll (float): roll angle
        pitch (float): pitch angle
        yaw (float): yaw angle

    Returns:
        rot_mat (torch.Tensor): rotation matix
    """
    roll = torch.tensor([roll])
    pitch = torch.tensor([pitch])
    yaw = torch.tensor([yaw])

    tensor_0 = torch.zeros(1)
    tensor_1 = torch.ones(1)

    rx = torch.stack(
        [
            torch.stack([tensor_1, tensor_0, tensor_0]),
            torch.stack([tensor_0, torch.cos(roll), -torch.sin(roll)]),
            torch.stack([tensor_0, torch.sin(roll), torch.cos(roll)]),
        ]
    ).reshape(3, 3)

    ry = torch.stack(
        [
            torch.stack([torch.cos(pitch), tensor_0, torch.sin(pitch)]),
            torch.stack([tensor_0, tensor_1, tensor_0]),
            torch.stack([-torch.sin(pitch), tensor_0, torch.cos(pitch)]),
        ]
    ).reshape(3, 3)

    rz = torch.stack(
        [
            torch.stack([torch.cos(yaw), -torch.sin(yaw), tensor_0]),
            torch.stack([torch.sin(yaw), torch.cos(yaw), tensor_0]),
            torch.stack([tensor_0, tensor_0, tensor_1]),
        ]
    ).reshape(3, 3)

    rot_mat = torch.mm(rz, ry)
    rot_mat = torch.mm(rot_mat, rx)
    return rot_mat


# @torch.jit.script
def inverse_homo(transform: torch.Tensor) -> torch.Tensor:
    """
    Compute inverse of homogeneous transformation matrix.

    The matrix should have entries
    [[R,       Rt]
     [0, 0, 0, 1]].

    Args:
        transform (torch.Tensor): _description_

    Returns:
        inverse_mat (torch.Tensor): inverse homography
    """
    rotation = transform[0:3, 0:3]
    trans = rotation.T @ transform[0:3, 3].reshape(3, 1)
    inverse_mat = torch.cat(
        [
            torch.cat([rotation.T, -trans], dim=1),
            torch.tensor([[0, 0, 0, 1]]).to(rotation.device),
        ],
        dim=0,
    )

    return inverse_mat


def transform_to_camera_frame(pts: torch.Tensor, poses: torch.Tensor) -> torch.Tensor:
    """
    Transform all point clouds into the frame of the most recent image

    Args:
        pts (torch.Tensor): points
        poses (torch.Tensor): poses

    Returns:
        pts (torch.Tensor): points, transformed
    """
    tf_i_to_n = inverse_homo(poses[-1]) @ poses
    pts = [
        transform_vec(pts[i].unsqueeze(0), tf_i_to_n[i].unsqueeze(0))[0]
        for i in range(len(pts))
    ]
    return pts


# @torch.jit.script
def downsample_xyz(pts: List[torch.Tensor], pts_per_frame: int) -> List[torch.Tensor]:
    """
    Downsample point clouds proportion of points.

    Args:
        pts (List[torch.Tensor]): input points
        pts_per_frame (int): numer of points to keep

    Returns:
        pts (List[torch.Tensor]): downsampled points
    """

    # NB: Will this result in same sampling distribution?
    for i, pt in enumerate(pts):
        nlen = len(pt)
        pts_to_keep = int(pts_per_frame / 90_000 * nlen)
        idxs = (
            torch.randperm(nlen, dtype=torch.int32, device=pt.device)[:pts_to_keep]
            .sort()[0]
            .long()
        )

        pts[i] = pts[i][idxs]

    return pts


def bound_point_cloud_world(
    pts: torch.Tensor, poses: torch.Tensor, world_bounds: torch.Tensor
):
    """
    Bound the point cloud in the world frame.

    Args:
        pts (torch.Tensor): input points
        poses (torch.Tensor): input poses
        world_bounds (torch.Tensor): input world bounds

    Returns:
        pts (torch.Tensor): Cropped points based on bounds
    """
    for i, pose in zip(range(len(pts)), poses):
        world_pc = transform_vec(pts[i].unsqueeze(0), pose.unsqueeze(0))[0]
        valid = torch.all(world_pc >= world_bounds[0], dim=1) & torch.all(
            world_pc <= world_bounds[1], dim=1
        )
        pts[i] = pts[i][valid]

    # ensure nonzero
    if sum(len(pt) for pt in pts) == 0:
        # print("No points in bounds")
        return None

    return pts


def transform_vec(x_vec: torch.Tensor, transform: torch.Tensor) -> torch.Tensor:
    """Transform 3D vector `x_vec` by homogenous transformation `tf`.

    Args:
        x (torch.Tensor): (b, ..., 3) coordinates in R3
        tf (torch.Tensor): (b, 4, 4) homogeneous pose matrix

    Returns:
        torch.Tensor: (b, ..., 3) coordinates of transformed vectors.
    """

    x_dim = len(x_vec.shape)
    assert all(
        (
            len(transform.shape) == 3,  # tf must be (b, 4, 4)
            transform.shape[1:] == (4, 4),  # tf must be (b, 4, 4)
            transform.shape[0] == x_vec.shape[0],  # batch dimension must be same
            x_dim > 2,  # x must be a batched matrix/tensor
        )
    ), "Argument shapes are unsupported."

    x_homog = torch.cat(
        [x_vec, torch.ones(*x_vec.shape[:-1], 1, device=x_vec.device)], dim=-1
    )

    # Pad the dimension of tf for broadcasting.
    # E.g., if x had shape (2, 3, 7, 3), and tf had shape (2, 4, 4), then
    # we reshape tf to (2, 1, 1, 4, 4)
    transform = transform.reshape(transform.shape[0], *([1] * (x_dim - 3)), 4, 4)

    return (x_homog @ transform.transpose(-2, -1))[..., :3]


# @torch.jit.script
def prepend_coordinate(matrix: torch.Tensor, coord: int):
    """Concatenate a constant column of value `coord` before a 2D matrix."""
    return torch.column_stack(
        [coord * torch.ones((len(matrix), 1), device=matrix.device), matrix]
    )


def unweighted_sum(coords: torch.Tensor):
    """
    Create a feature vector from a coordinate array, so each
    row's feature is the number of rows that share that coordinate.

    Args:
        coords (torch.Tensor): coordinates

    Returns:
        torch.Tensor: _description_
    """

    _, idcs, counts = coords.unique(dim=0, return_counts=True, return_inverse=True)
    features = counts[idcs]
    return features.reshape(-1, 1).to(torch.float32)


# @torch.jit.script
def discretize(positions: torch.Tensor, grid_size: float) -> torch.Tensor:
    """
    Truncate each position to an integer grid.

    Args:
        positions (torch.Tensor): _description_
        grid_size (float): _description_

    Returns:
        torch.Tensor: _description_
    """
    return (positions / grid_size).int()


def infer_grasps(
    tsgraspnet, points: List[torch.Tensor], grid_size: float
) -> torch.Tensor:
    """
    Run a sparse convolutional network on a list of
    consecutive point clouds, and return the grasp predictions for the last point cloud.
    Each point cloud may have different numbers of points.

    Args:
        tsgraspnet (_type_): _description_
        points (List[torch.Tensor]): _description_
        grid_size (float): _description_

    Returns:
        torch.Tensor: _description_
    """

    list_coords = [prepend_coordinate(pt, idx) for idx, pt in enumerate(points)]
    coords = torch.cat(list_coords, dim=0)
    coords = prepend_coordinate(coords, 0)  # add dummy batch dimension

    # Discretize coordinates to integer grid
    coords = discretize(coords, grid_size).contiguous()
    feats = unweighted_sum(coords)

    # Construct a Minkoswki sparse tensor and run forward inference
    stensor = MinkowskiEngine.SparseTensor(coordinates=coords, features=feats)

    (
        class_logits,
        baseline_dir,
        approach_dir,
        grasp_offset,
    ) = tsgraspnet.model.forward(stensor)

    # Return the grasp predictions for the latest point cloud
    # Find the maximum value in the second column
    max_value = coords[:, 1].max()
    idcs = coords[:, 1] == max_value

    return (
        class_logits[idcs],
        baseline_dir[idcs],
        approach_dir[idcs],
        grasp_offset[idcs],
        points[-1],
    )


# @torch.jit.script
def build_6dof_grasps(
    contact_pts,
    baseline_dir,
    approach_dir,
    grasp_width,
    gripper_depth: float,
):
    """
    Calculate the SE(3) transforms corresponding to each predicted coord/approach/baseline/grasp_width grasp.

    Unbatched for torch.jit.script.

    Args:
        contact_pts (torch.Tensor): (N, 3) contact points predicted
        baseline_dir (torch.Tensor): (N, 3) gripper baseline directions
        approach_dir (torch.Tensor): (N, 3) gripper approach directions
        grasp_width (torch.Tensor): (N, 3) gripper width

    Returns:
        pred_grasp_tfs (torch.Tensor): (N, 4, 4) homogeneous grasp poses.
    """
    nn = contact_pts.shape[0]
    grasps_r = torch.stack(
        [baseline_dir, torch.cross(approach_dir, baseline_dir), approach_dir], dim=-1
    )
    grasps_t = (
        contact_pts + grasp_width / 2 * baseline_dir - gripper_depth * approach_dir
    )
    ones = torch.ones((nn, 1, 1), device=contact_pts.device)
    zeros = torch.zeros((nn, 1, 3), device=contact_pts.device)
    homog_vec = torch.cat([zeros, ones], dim=-1)

    pred_grasp_tfs = torch.cat(
        [torch.cat([grasps_r, grasps_t.unsqueeze(-1)], dim=-1), homog_vec], dim=-2
    )
    return pred_grasp_tfs


def se3_dist(pose_1, pose_2):
    """
    'Distance' between two poses. Presently, just gives R(3) distance.
    """
    distance = np.linalg.norm(
        np.array(
            [
                pose_2.position.x - pose_1.position.x,
                pose_2.position.y - pose_1.position.y,
                pose_2.position.z - pose_1.position.z,
            ]
        )
    )

    return distance