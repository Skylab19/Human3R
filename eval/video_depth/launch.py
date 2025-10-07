import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
import math
import cv2
import numpy as np
import torch
import argparse

from copy import deepcopy
from eval.video_depth.metadata import dataset_metadata
from eval.video_depth.utils import save_depth_maps
from accelerate import PartialState
from add_ckpt_path import add_path_to_dust3r
import time
from tqdm import tqdm


def get_args_parser():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--weights",
        type=str,
        help="path to the model weights",
        default="",
    )

    parser.add_argument("--device", type=str, default="cuda", help="pytorch device")
    parser.add_argument(
        "--output_dir",
        type=str,
        default="",
        help="value for outdir",
    )
    parser.add_argument(
        "--no_crop", type=bool, default=True, help="whether to crop input data"
    )

    parser.add_argument(
        "--eval_dataset",
        type=str,
        default="sintel",
        choices=list(dataset_metadata.keys()),
    )
    parser.add_argument("--size", type=int, default="224")

    parser.add_argument(
        "--pose_eval_stride", default=1, type=int, help="stride for pose evaluation"
    )
    parser.add_argument(
        "--full_seq",
        action="store_true",
        default=False,
        help="use full sequence for pose evaluation",
    )
    parser.add_argument(
        "--seq_list",
        nargs="+",
        default=None,
        help="list of sequences for pose evaluation",
    )
    parser.add_argument("--use_ttt3r", action="store_true", default=False)
    parser.add_argument("--reset_interval", type=int, default=100000000)
    return parser


def eval_pose_estimation(args, model, save_dir=None):
    metadata = dataset_metadata.get(args.eval_dataset)
    img_path = metadata["img_path"]
    mask_path = metadata["mask_path"]

    ate_mean, rpe_trans_mean, rpe_rot_mean = eval_pose_estimation_dist(
        args, model, save_dir=save_dir, img_path=img_path, mask_path=mask_path
    )
    return ate_mean, rpe_trans_mean, rpe_rot_mean


def eval_pose_estimation_dist(args, model, img_path, save_dir=None, mask_path=None):
    from dust3r.inference import inference, inference_recurrent_lighter

    metadata = dataset_metadata.get(args.eval_dataset)
    anno_path = metadata.get("anno_path", None)

    seq_list = args.seq_list
    if seq_list is None:
        if metadata.get("full_seq", False):
            args.full_seq = True
        else:
            seq_list = metadata.get("seq_list", [])
        if args.full_seq:
            seq_list = os.listdir(img_path)
            seq_list = [
                seq for seq in seq_list if os.path.isdir(os.path.join(img_path, seq))
            ]
        seq_list = sorted(seq_list)

    if save_dir is None:
        save_dir = args.output_dir

    distributed_state = PartialState()
    model.to(distributed_state.device)
    device = distributed_state.device
    img_res = getattr(model, 'mhmr_img_res', None)

    with distributed_state.split_between_processes(seq_list) as seqs:
        ate_list = []
        rpe_trans_list = []
        rpe_rot_list = []
        load_img_size = args.size
        assert load_img_size == 512
        error_log_path = f"{save_dir}/_error_log_{distributed_state.process_index}.txt"  # Unique log file per process
        bug = False
        for seq in tqdm(seqs):
            try:
                dir_path = metadata["dir_path_func"](img_path, seq)

                # Handle skip_condition
                skip_condition = metadata.get("skip_condition", None)
                if skip_condition is not None and skip_condition(save_dir, seq):
                    continue

                mask_path_seq_func = metadata.get(
                    "mask_path_seq_func", lambda mask_path, seq: None
                )
                mask_path_seq = mask_path_seq_func(mask_path, seq)

                filelist = [
                    os.path.join(dir_path, name) for name in os.listdir(dir_path)
                ]
                filelist.sort()
                filelist = filelist[:: args.pose_eval_stride]

                views = prepare_input(
                    filelist,
                    [True for _ in filelist],
                    size=load_img_size,
                    crop=not args.no_crop,
                    img_res=img_res,
                    reset_interval=args.reset_interval,
                )
                start = time.time()
                # outputs, _ = inference(views, model, device)
                outputs, _ = inference_recurrent_lighter(
                    views, model, device, verbose=False, use_ttt3r=args.use_ttt3r)
                end = time.time()
                fps = len(filelist) / (end - start)

                (
                    colors,
                    pts3ds_self,
                    pts3ds_other,
                    conf_self,
                    conf_other,
                    cam_dict,
                    pr_poses,
                ) = prepare_output(outputs)

                os.makedirs(f"{save_dir}/{seq}", exist_ok=True)
                save_depth_maps(pts3ds_self, f"{save_dir}/{seq}", conf_self=conf_self)

            except Exception as e:
                if "out of memory" in str(e):
                    # Handle OOM
                    torch.cuda.empty_cache()  # Clear the CUDA memory
                    with open(error_log_path, "a") as f:
                        f.write(
                            f"OOM error in sequence {seq}, skipping this sequence.\n"
                        )
                    print(f"OOM error in sequence {seq}, skipping...")
                elif "Degenerate covariance rank" in str(
                    e
                ) or "Eigenvalues did not converge" in str(e):
                    # Handle Degenerate covariance rank exception and Eigenvalues did not converge exception
                    with open(error_log_path, "a") as f:
                        f.write(f"Exception in sequence {seq}: {str(e)}\n")
                    print(f"Traj evaluation error in sequence {seq}, skipping.")
                else:
                    raise e  # Rethrow if it's not an expected exception
    return None, None, None


if __name__ == "__main__":
    args = get_args_parser()
    args = args.parse_args()
    add_path_to_dust3r(args.weights)
    from dust3r.utils.image import load_images_for_eval as load_images
    from src.dust3r.utils.image import pad_image
    from dust3r.post_process import estimate_focal_knowing_depth
    from dust3r.model import ARCroco3DStereo
    from dust3r.utils.camera import pose_encoding_to_camera
    from dust3r.utils.geometry import matrix_cumprod, get_camera_parameters

    if args.eval_dataset == "sintel":
        args.full_seq = True
    else:
        args.full_seq = False
    args.no_crop = True

    def prepare_input(
        img_paths,
        img_mask,
        size,
        raymaps=None,
        raymap_mask=None,
        revisit=1,
        update=True,
        crop=True,
        img_res=None, 
        reset_interval=100,
    ):
        images = load_images(img_paths, size=size, crop=crop)
        if img_res is not None:
            K_mhmr = get_camera_parameters(img_res, device="cpu") # if use pseudo K

        views = []
        if raymaps is None and raymap_mask is None:
            num_views = len(images)
            # Only images are provided.
            for i in range(num_views):
                view = {
                    "img": images[i]["img"],
                    "ray_map": torch.full(
                        (
                            images[i]["img"].shape[0],
                            6,
                            images[i]["img"].shape[-2],
                            images[i]["img"].shape[-1],
                        ),
                        torch.nan,
                    ),
                    "true_shape": torch.from_numpy(images[i]["true_shape"]),
                    "idx": i,
                    "instance": str(i),
                    "camera_pose": torch.from_numpy(
                        np.eye(4).astype(np.float32)
                    ).unsqueeze(0),
                    "img_mask": torch.tensor(True).unsqueeze(0),
                    "ray_mask": torch.tensor(False).unsqueeze(0),
                    "update": torch.tensor(True).unsqueeze(0),
                    # "reset": torch.tensor(False).unsqueeze(0),
                    "reset": torch.tensor((i+1) % reset_interval == 0).unsqueeze(0),
                }
                if img_res is not None:
                    view["img_mhmr"] = pad_image(view["img"], img_res)
                    view["K_mhmr"] = K_mhmr
                views.append(view)
                if (i+1) % reset_interval == 0:
                    overlap_view = deepcopy(view)
                    overlap_view["reset"] = torch.tensor(False).unsqueeze(0)
                    views.append(overlap_view)
        else:
            # Combine images and raymaps.
            num_views = len(images) + len(raymaps)
            assert len(img_mask) == len(raymap_mask) == num_views
            assert sum(img_mask) == len(images) and sum(raymap_mask) == len(raymaps)

            j = 0
            k = 0
            for i in range(num_views):
                view = {
                    "img": (
                        images[j]["img"]
                        if img_mask[i]
                        else torch.full_like(images[0]["img"], torch.nan)
                    ),
                    "ray_map": (
                        raymaps[k]
                        if raymap_mask[i]
                        else torch.full_like(raymaps[0], torch.nan)
                    ),
                    "true_shape": (
                        torch.from_numpy(images[j]["true_shape"])
                        if img_mask[i]
                        else torch.from_numpy(np.int32([raymaps[k].shape[1:-1][::-1]]))
                    ),
                    "idx": i,
                    "instance": str(i),
                    "camera_pose": torch.from_numpy(
                        np.eye(4).astype(np.float32)
                    ).unsqueeze(0),
                    "img_mask": torch.tensor(img_mask[i]).unsqueeze(0),
                    "ray_mask": torch.tensor(raymap_mask[i]).unsqueeze(0),
                    "update": torch.tensor(img_mask[i]).unsqueeze(0),
                    # "reset": torch.tensor(False).unsqueeze(0),
                    "reset": torch.tensor((i+1) % reset_interval == 0).unsqueeze(0),
                }
                if img_res is not None:
                    view["img_mhmr"] = pad_image(view["img"], img_res)
                    view["K_mhmr"] = K_mhmr
                if img_mask[i]:
                    j += 1
                if raymap_mask[i]:
                    k += 1
                views.append(view)
                if (i+1) % reset_interval == 0:
                    overlap_view = deepcopy(view)
                    overlap_view["reset"] = torch.tensor(False).unsqueeze(0)
                    views.append(overlap_view)
            assert j == len(images) and k == len(raymaps)

        if revisit > 1:
            # repeat input for 'revisit' times
            new_views = []
            for r in range(revisit):
                for i in range(len(views)):
                    new_view = deepcopy(views[i])
                    new_view["idx"] = r * len(views) + i
                    new_view["instance"] = str(r * len(views) + i)
                    if r > 0:
                        if not update:
                            new_view["update"] = torch.tensor(False).unsqueeze(0)
                    new_views.append(new_view)
            return new_views
        return views

    def prepare_output(outputs, revisit=1):
        valid_length = len(outputs["pred"]) // revisit
        outputs["pred"] = outputs["pred"][-valid_length:]
        outputs["views"] = outputs["views"][-valid_length:]

        # delet overlaps: reset_mask=True outputs["pred"] and outputs["views"]
        reset_mask = torch.cat([view["reset"] for view in outputs["views"]], 0)
        shifted_reset_mask = torch.cat([torch.tensor(False).unsqueeze(0), reset_mask[:-1]], dim=0)
        outputs["pred"] = [
            pred for pred, mask in zip(outputs["pred"], shifted_reset_mask) if not mask]
        reset_mask = reset_mask[~shifted_reset_mask]

        pts3ds_self = [output["pts3d_in_self_view"].cpu() for output in outputs["pred"]]
        pts3ds_other = [
            output["pts3d_in_other_view"].cpu() for output in outputs["pred"]
        ]
        conf_self = [output["conf_self"].cpu() for output in outputs["pred"]]
        conf_other = [output["conf"].cpu() for output in outputs["pred"]]
        pts3ds_self = torch.cat(pts3ds_self, 0)
        pr_poses = [
            pose_encoding_to_camera(pred["camera_pose"].clone()).cpu()
            for pred in outputs["pred"]
        ]
        pr_poses = torch.cat(pr_poses, 0)

        B, H, W, _ = pts3ds_self.shape
        pp = (
            torch.tensor([W // 2, H // 2], device=pts3ds_self.device)
            .float()
            .repeat(B, 1)
            .reshape(B, 2)
        )
        focal = estimate_focal_knowing_depth(pts3ds_self, pp, focal_mode="weiszfeld")

        if reset_mask.any():
            identity = torch.eye(4, device=pr_poses.device)
            reset_poses = torch.where(reset_mask.unsqueeze(-1).unsqueeze(-1), pr_poses, identity)
            cumulative_bases = matrix_cumprod(reset_poses)
            shifted_bases = torch.cat([identity.unsqueeze(0), cumulative_bases[:-1]], dim=0)
            pr_poses = torch.einsum('bij,bjk->bik', shifted_bases, pr_poses)

        colors = [0.5 * (output["rgb"][0] + 1.0) for output in outputs["pred"]]
        cam_dict = {
            "focal": focal.cpu().numpy(),
            "pp": pp.cpu().numpy(),
        }
        return (
            colors,
            pts3ds_self,
            pts3ds_other,
            conf_self,
            conf_other,
            cam_dict,
            pr_poses,
        )

    model = ARCroco3DStereo.from_pretrained(args.weights)
    eval_pose_estimation(args, model, save_dir=args.output_dir)
