"""
train_cgn_ours.py
Script to train contact graspnet on our data.
"""

## CGN imports
import typing
import contact_graspnet.pointnet2.models
import contact_graspnet.pointnet2.utils
from contact_graspnet.contact_graspnet import config_utils
from contact_graspnet.contact_graspnet.data import PointCloudReader, load_scene_contacts, center_pc_convert_cam
from contact_graspnet.contact_graspnet.summaries import build_summary_ops, build_file_writers
from contact_graspnet.contact_graspnet.tf_train_ops import load_labels_and_losses, build_train_op, get_bn_decay
from contact_graspnet.contact_graspnet.contact_grasp_estimator import GraspEstimator
import contact_graspnet.contact_graspnet.contact_graspnet as cgn_module

# hack exposing group_point function without reimporting with statement below
# from contact_graspnet.pointnet2.tf_ops.grouping.tf_grouping import group_point
group_point = cgn_module.group_point

## Tensorflow imports
import tensorflow.compat.v1 as tf
tf.disable_eager_execution()
TF2 = True
physical_devices = tf.config.experimental.list_physical_devices('GPU')
tf.config.experimental.set_memory_growth(physical_devices[0], True)

## tsgrasp imports
from tsgrasp.data.lit_acronymvid import LitAcronymvidDataset
import torch

def get_cgn_config():
    cfg = config_utils.load_config(
        checkpoint_dir="/home/tim/Research/tsgrasp/ckts/cgn_ckpts"
    )
    return cfg

def train_dataloader(batch_size: int) -> LitAcronymvidDataset:
    from hydra import compose, initialize
    from torch.utils.data import DataLoader

    with initialize(config_path="../conf/data"):
        cfg = compose(config_name="acronymvid")

    lds = LitAcronymvidDataset(cfg.data.data_cfg, batch_size=batch_size)
    lds.prepare_data()
    lds.setup()

    # create DataLoader with special collate function, not minkowski_collate,
    # so we don't have to import ME
    def collate_fn(list_data):
        from tsgrasp.data.acronymvid import collate_control_points

            ## Each batch may have different numbers of ground truth grasps, resulting in ragged tensors. We require even, rectangular tensors for calculating the ADD-S loss, so we collate them into rectangular tensors.
        pos_control_points, sym_pos_control_points, gt_grasps_per_batch = \
            collate_control_points(
            batch = torch.arange(len(list_data)),
            time = torch.stack([d["coordinates"][:,0] for d in list_data]),
            pos_cp_list = [d["pos_control_points"] for d in list_data],
            sym_pos_cp_list = [d["sym_pos_control_points"] for d in list_data]
        )

        return {
            "positions": torch.stack([d["positions"] for d in list_data]),
            "labels": torch.stack([d["labels"] for d in list_data]),
            "pos_control_points": pos_control_points,
            "sym_pos_control_points": sym_pos_control_points,
            "gt_grasps_per_batch": gt_grasps_per_batch,
            "cam_frame_pos_grasp_tfs": [d["cam_frame_pos_grasp_tfs"] for d in list_data],
            "pos_contact_pts_mesh": [d["pos_contact_pts_mesh"] for d in list_data],
            "pos_finger_diffs": [d["pos_finger_diffs"] for d in list_data],
            "camera_pose": [d["camera_pose"] for d in list_data]
        }

    dl = DataLoader(
        lds.dataset_train,
        batch_size=batch_size,
        shuffle=True, 
        collate_fn=collate_fn
    )
    return dl

class CGNWrapper:

    def __init__(self, global_config, batch_size=3, num_input_points=20000, num_target_points=2048):
        self.global_config = global_config
        self.input_pl = self.placeholder_inputs(
            batch_size=batch_size,
            num_input_points=num_input_points,
            input_normals=False
        )
        self.step = tf.Variable(0)
        self.end_points = cgn_module.get_model(
            point_cloud = self.input_pl['pointclouds_pl'],
            is_training = self.input_pl['is_training_pl'],
            global_config = self.global_config,
            bn_decay = get_bn_decay(self.step, global_config['OPTIMIZER'])
        )
        self.data_pl = self.placeholder_data(b=batch_size, M=None)
        (
            dir_labels_pc_cam,
            offset_labels_pc,
            grasp_success_labels_pc,
            approach_labels_pc_cam
        ) = self.compute_labels(
            pos_contact_pts_mesh=self.data_pl['pos_contact_pts_mesh'],
            pos_grasp_tfs_cam=self.data_pl['pos_grasp_tfs_cam'],
            pos_finger_diffs=self.data_pl['pos_finger_diffs'],
            pc_cam_pl=self.end_points['pred_points'],
            camera_pose_pl=self.data_pl['camera_pose_pl'],
            global_config=global_config
        )
        self.losses = self.get_losses(dir_labels_pc_cam, offset_labels_pc,
            grasp_success_labels_pc, approach_labels_pc_cam)
        self.train_op = build_train_op(self.losses['loss'], self.step, global_config)

    def get_losses(self, dir_labels_pc_cam, offset_labels_pc, grasp_success_labels_pc, approach_labels_pc_cam):
        (
            dir_loss, 
            bin_ce_loss, 
            offset_loss, 
            approach_loss, 
            adds_loss, 
            adds_loss_gt2pred 
        )= cgn_module.get_losses(
            self.end_points['pred_points'],
            self.end_points,
            dir_labels_pc_cam,
            offset_labels_pc,
            grasp_success_labels_pc,
            approach_labels_pc_cam,
            self.global_config
        )

        total_loss = 0
        if self.global_config['MODEL']['pred_contact_base']:
            total_loss += self.global_config['OPTIMIZER']['dir_cosine_loss_weight'] * dir_loss
        if self.global_config['MODEL']['pred_contact_success']:
            total_loss += self.global_config['OPTIMIZER']['score_ce_loss_weight'] * bin_ce_loss
        if self.global_config['MODEL']['pred_contact_offset']:
            total_loss += self.global_config['OPTIMIZER']['offset_loss_weight'] * offset_loss
        if self.global_config['MODEL']['pred_contact_approach']:
            total_loss += self.global_config['OPTIMIZER']['approach_cosine_loss_weight'] * approach_loss
        if self.global_config['MODEL']['pred_grasps_adds']:
            total_loss += self.global_config['OPTIMIZER']['adds_loss_weight'] * adds_loss
        if self.global_config['MODEL']['pred_grasps_adds_gt2pred']:
            total_loss += self.global_config['OPTIMIZER']['adds_gt2pred_loss_weight'] * adds_loss_gt2pred

        return {
            'loss': total_loss,
            'dir_loss': dir_loss,
            'bin_ce_loss': bin_ce_loss,
            'offset_loss': offset_loss,
            'approach_loss': approach_loss,
            'adds_loss': adds_loss,
            'adds_gt2pred_loss': adds_loss_gt2pred
        }

    @staticmethod
    def placeholder_inputs(batch_size, num_input_points, input_normals):
        """
        Creates placeholders for input pointclouds and training/eval mode 

        Arguments:
            batch_size {int} -- batch size
            num_input_points {int} -- number of input points to the network (default: 20000)

        Keyword Arguments:
            input_normals {bool} -- whether to use normals as input (default: {False})

        Returns:
            dict[str:tf.placeholder] -- dict of placeholders
        """
        pl_dict = {}
        dim = 6 if input_normals else 3
        pl_dict['pointclouds_pl'] = tf.placeholder(
            tf.float32, shape=(batch_size, num_input_points, dim)
        )
        pl_dict['is_training_pl'] = tf.placeholder(tf.bool, shape=())

        return pl_dict
        
    @staticmethod
    def placeholder_data(b, M):
        """b batches, M contact points"""
        return {
            "pos_contact_pts_mesh": tf.placeholder(
                tf.float32, shape=(M,2,3)
            ),
            "pos_grasp_tfs_cam": tf.placeholder(
                tf.float32, shape=(b, M,4,4)
            ),
            "pos_finger_diffs": tf.placeholder(
                tf.float32, shape=(M,1)
            ),
            "camera_pose_pl": tf.placeholder(
                tf.float32, shape=(b,4,4)
            )
        }

    @staticmethod
    def compute_labels(pos_contact_pts_mesh, pos_grasp_tfs_cam, pos_finger_diffs, pc_cam_pl, camera_pose_pl, global_config):
        """
        Project grasp labels defined on meshes onto rendered point cloud from a camera pose via nearest neighbor contacts within a maximum radius. 
        All points without nearby successful grasp contacts are considered negative contact points.

        Arguments:
            pos_contact_pts_mesh {tf.constant} -- positive contact points on the mesh scene (Mx3)
            pos_grasp_tfs_cam {tf.placeholder} -- positive grasp transforms in the camera frame (bxMx4x4)
            pos_contact_dirs_mesh {tf.constant} -- respective contact base directions in the mesh scene (Mx3)
            pos_contact_approaches_mesh {tf.constant} -- respective contact approach directions in the mesh scene (Mx3)
            pos_finger_diffs {tf.constant} -- respective grasp widths in the mesh scene (Mx1)
            pc_cam_pl {tf.placeholder} -- bxNx3 rendered point clouds
            camera_pose_pl {tf.placeholder} -- bx4x4 camera poses
            global_config {dict} -- global config

        Returns:
            [dir_labels_pc_cam, offset_labels_pc, grasp_success_labels_pc, approach_labels_pc_cam] -- Per-point contact success labels and per-contact pose labels in rendered point cloud
        """
        label_config = global_config['DATA']['labels']
        model_config = global_config['MODEL']

        nsample = label_config['k']
        radius = label_config['max_radius']
        filter_z = label_config['filter_z']
        z_val = label_config['z_val']

        xyz_cam = pc_cam_pl[:,:,:3]

        # Repeat finger diffs along batch dimension
        contact_point_offsets_batch = tf.keras.backend.repeat_elements(
            tf.expand_dims(pos_finger_diffs,0), xyz_cam.shape[0], axis=0)

        # pos_grasp_tfs_cam {tf.placeholder} -- positive grasp transforms in the camera frame (bxM/2x4x4)
        # The contact direction IS the pose transform's x axis, right?
        contact_point_dirs_batch_cam = pos_grasp_tfs_cam[:, :, :3, 0]

        # Repeat approach dirs along batch dimensions
        pos_contact_approaches_batch_cam = pos_grasp_tfs_cam[:, :, :3, 2]

        # (M, 2, 3) -> (2M, 3)
        contact_points_mesh = tf.reshape(
            pos_contact_pts_mesh, (-1, 3)
        )
        contact_point_batch_mesh = tf.keras.backend.repeat_elements(
            tf.expand_dims(contact_points_mesh, 0),
            xyz_cam.shape[0],
            axis=0
        )

        # Transform contact points into camera frame
        pad_homog2 = tf.ones(
            (xyz_cam.shape[0], tf.shape(contact_point_batch_mesh)[1], 1)
        )
        contact_point_batch_cam = tf.matmul(
            tf.concat([contact_point_batch_mesh, pad_homog2], 2), 
            tf.transpose(camera_pose_pl, perm=[0, 2, 1])
        )[:,:,:3]

        if filter_z:
            # Remove points with z value too low
            dir_filter_passed = tf.keras.backend.repeat_elements(
                tf.math.greater(
                    contact_point_dirs_batch_cam[:,:,2:3], 
                    tf.constant([z_val])
                ), 3, axis=2
            )
            contact_point_batch_mesh = tf.where(
                dir_filter_passed, contact_point_batch_mesh, 
                tf.ones_like(contact_point_batch_mesh)*100000
            )

        # Find pairwise distance between mesh points and camera point cloud
        squared_dists_all = tf.reduce_sum(
            (
                tf.expand_dims(contact_point_batch_cam,1) -
                tf.expand_dims(xyz_cam,2)
            )**2,
            axis=3
        )

        # Get indices of closest distances
        neg_squared_dists_k, close_contact_pt_idcs = tf.math.top_k(
            -squared_dists_all, k=nsample, sorted=False)

        squared_dists_k = -neg_squared_dists_k

        # Nearest neighbor mapping
        # label points true of the average top-k distance is less than radius
        grasp_success_labels_pc = tf.cast(
            tf.less(
                tf.reduce_mean(squared_dists_k, axis=2), 
                radius*radius
            ), tf.float32
        ) # (batch_size, num_point)

        # Get values of dirs, approaches, and offsets at nearest indices
        grouped_dirs_pc_cam = group_point(
            contact_point_dirs_batch_cam, close_contact_pt_idcs
        )
        grouped_approaches_pc_cam = group_point(
            pos_contact_approaches_batch_cam, close_contact_pt_idcs
        )
        grouped_offsets = group_point(
            tf.expand_dims(contact_point_offsets_batch,2), close_contact_pt_idcs
        )

        # Take average values if multiple were selected
        dir_labels_pc_cam = tf.math.l2_normalize(
            tf.reduce_mean(grouped_dirs_pc_cam, axis=2),
            axis=2
        ) # (batch_size, num_point, 3)
        approach_labels_pc_cam = tf.math.l2_normalize(
            tf.reduce_mean(grouped_approaches_pc_cam, axis=2)
            ,axis=2
        ) # (batch_size, num_point, 3)
        offset_labels_pc = tf.reduce_mean(grouped_offsets, axis=2)
            
        return dir_labels_pc_cam, offset_labels_pc, grasp_success_labels_pc, approach_labels_pc_cam
    
def train(global_config):

    # for the "batch" dimension we are using the time dimension of the examples
    dl = train_dataloader(batch_size=1)
    with tf.Graph().as_default(): # create a new graph with this scope

        cgn = CGNWrapper(global_config, batch_size=3)

        # Add ops to save and restore all the variables.
        saver = tf.train.Saver(save_relative_paths=True, keep_checkpoint_every_n_hours=4)

        # Create a session
        config = tf.ConfigProto()
        config.gpu_options.allow_growth = True
        config.allow_soft_placement = True
        # config = tf.ConfigProto(
        #     device_count = {'GPU': 0}
        # )
        sess = tf.Session(config=config)
        # from tensorflow.python import debug as tf_debug
        # sess = tf_debug.LocalCLIDebugWrapperSession(sess)

        # Log summaries
        # This logs everything, but I don't want to right now.
        # TODO: create our own "build_summary_ops" with just what we need.
        # summary_ops = build_summary_ops(ops, sess, global_config)

        # Init/Load weights
        # TODO: add weight loading
        sess.run(tf.global_variables_initializer())

    ## Run training op
    for i, data in enumerate(dl):
        print(f"**** Iteration {i} ****")
        # Inference on a batchdata = next(iter(dl))

        feed_dict = {
            # input variables
            cgn.input_pl['pointclouds_pl']: data['positions'][0, :3, range(20000)], # TODO: use proper idxs
            cgn.input_pl['is_training_pl']: True,
            # variables that we turned into placeholders
            # TODO: see contact_graspnet.py::compute_labels to determine HOW MANY of the ground truth labels are passed along, and how (if at all)
            # they are sampled.
            cgn.data_pl['pos_contact_pts_mesh']: data['pos_contact_pts_mesh'][0],
            cgn.data_pl['pos_grasp_tfs_cam']: data['cam_frame_pos_grasp_tfs'][0][:3],
            # TODO finger_diffs has nan -- uh oh
            # TODO should this be one dimensional?
            cgn.data_pl['pos_finger_diffs']: data['pos_finger_diffs'][0],
            cgn.data_pl['camera_pose_pl']: data['camera_pose'][0][:3],
        }
        (   
            _,
            step, 
            loss_val, 
            dir_loss, 
            bin_ce_loss, 
            # offset_loss, 
            # approach_loss,
            adds_loss, 
            adds_gt2pred_loss
        ) = sess.run([
                cgn.train_op,
                cgn.step, 
                cgn.losses['loss'], 
                cgn.losses['dir_loss'], 
                cgn.losses['bin_ce_loss'], 
                cgn.losses['adds_loss'],
                cgn.losses['adds_gt2pred_loss']
            ], 
            feed_dict=feed_dict
        )

        print(
            (   
            # scene_idx, 
            step, 
            loss_val, 
            dir_loss, 
            bin_ce_loss, 
            # offset_loss, 
            # approach_loss,
            adds_loss, 
            adds_gt2pred_loss
        )
        )
        breakpoint()
        print("cat")
        # Compute loss

        # Update parameters

        # Log, etc.

def main():

    ge_global_config = get_cgn_config()
    ge_global_config['MODEL']['model'] = 'contact_graspnet.contact_graspnet.contact_graspnet'
    
    train(ge_global_config)

if __name__ == "__main__":
    main()