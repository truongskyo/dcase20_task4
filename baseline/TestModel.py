# -*- coding: utf-8 -*-
import argparse
import functools
import multiprocessing
import os.path as osp
from contextlib import closing

import torch
from torch.utils.data import DataLoader
import numpy as np
import pandas as pd

from data_utils.DataLoad import DataLoadDf
from data_utils.Desed import DESED
from evaluation_measures import psds_score, get_predictions, \
    compute_psds_from_operating_points, compute_metrics, compute_sed_eval_metrics
from utilities.utils import to_cuda_if_available, generate_tsv_wav_durations, meta_path_to_audio_dir
from utilities.ManyHotEncoder import ManyHotEncoder
from utilities.Transforms import get_transforms
from utilities.Logger import create_logger
from utilities.Scaler import Scaler, ScalerPerAudio
from models.CRNN import CRNN
import config as cfg

logger = create_logger(__name__)
torch.manual_seed(2020)


def _load_crnn(state, model_name="model"):
    crnn_args = state[model_name]["args"]
    crnn_kwargs = state[model_name]["kwargs"]
    crnn = CRNN(*crnn_args, **crnn_kwargs)
    crnn.load_state_dict(state[model_name]["state_dict"])
    crnn.eval()
    crnn = to_cuda_if_available(crnn)
    logger.info("Model loaded at epoch: {}".format(state["epoch"]))
    logger.info(crnn)
    return crnn


def _load_scaler(state):
    scaler_state = state["scaler"]
    type_sc = scaler_state["type"]
    if type_sc == "ScalerPerAudio":
        scaler = ScalerPerAudio(*scaler_state["args"])
    elif type_sc == "Scaler":
        scaler = Scaler()
    else:
        raise NotImplementedError("Not the right type of Scaler has been saved in state")
    scaler.load_state_dict(state["scaler"]["state_dict"])
    return scaler


def _load_state_vars(state, gtruth_df, median_win=None):
    pred_df = gtruth_df.copy()
    # Define dataloader
    many_hot_encoder = ManyHotEncoder.load_state_dict(state["many_hot_encoder"])
    scaler = _load_scaler(state)
    crnn = _load_crnn(state)
    transforms_valid = get_transforms(cfg.max_frames, scaler=scaler, add_axis=0)

    strong_dataload = DataLoadDf(pred_df, many_hot_encoder.encode_strong_df, transforms_valid, return_indexes=True)
    strong_dataloader_ind = DataLoader(strong_dataload, batch_size=cfg.batch_size, drop_last=False)

    pooling_time_ratio = state["pooling_time_ratio"]
    many_hot_encoder = ManyHotEncoder.load_state_dict(state["many_hot_encoder"])
    if median_win is None:
        median_win = state["median_window"]
    return {
        "model": crnn,
        "dataloader": strong_dataloader_ind,
        "pooling_time_ratio": pooling_time_ratio,
        "many_hot_encoder": many_hot_encoder,
        "median_window": median_win
    }


def get_variables(args):
    model_pth = args.model_path
    gt_fname, ext = osp.splitext(args.groundtruth_tsv)
    median_win = args.median_window
    meta_gt = args.meta_gt
    gt_audio_pth = args.groundtruth_audio_dir

    if meta_gt is None:
        meta_gt = gt_fname + "_durations" + ext

    if gt_audio_pth is None:
        gt_audio_pth = meta_path_to_audio_dir(gt_fname)
        # Useful because of the data format
        if "validation" in gt_audio_pth:
            gt_audio_pth = osp.dirname(gt_audio_pth)

    groundtruth = pd.read_csv(args.groundtruth_tsv, sep="\t")
    if osp.exists(meta_gt):
        meta_dur_df = pd.read_csv(meta_gt, sep='\t')
        if len(meta_dur_df) == 0:
            meta_dur_df = generate_tsv_wav_durations(gt_audio_pth, meta_gt)
    else:
        meta_dur_df = generate_tsv_wav_durations(gt_audio_pth, meta_gt)

    return model_pth, median_win, gt_audio_pth, groundtruth, meta_dur_df


class NoDaemonProcess(multiprocessing.Process):
    # make 'daemon' attribute always return False
    def _get_daemon(self):
        return False

    def _set_daemon(self, value):
        pass
    daemon = property(_get_daemon, _set_daemon)


class MyPool(multiprocessing.pool.Pool):
    # We sub-class multiprocessing.pool.Pool instead of multiprocessing.Pool
    # because the latter is only a wrapper function, not a proper class.
    Process = NoDaemonProcess


def bootstrap_iter(pred, gtruth, metric, frac, _, **kwargs):
    names_kept = gtruth.filename.drop_duplicates().sample(frac=frac)
    if isinstance(pred, list):
        pred_bt = []
        for pdf in pred:
            pred_bt.append(pdf[pdf.filename.isin(names_kept)])
    else:
        pred_bt = pred[pred.filename.isin(names_kept)]
    gt_bt = gtruth[gtruth.filename.isin(names_kept)]
    m = metric(pred_bt, gt_bt, **kwargs)
    return m


def bootstrap(pred, gtruth, metric, n_iterations=200, frac=0.8, confidence=0.9, **kwargs):
    bt_iter = functools.partial(bootstrap_iter, pred, gtruth, metric, frac, **kwargs)
    with closing(MyPool(multiprocessing.cpu_count() - 1)) as pool:
        result_metrics = pool.map(bt_iter, range(n_iterations))
    result_metrics.sort()
    mean_val = np.mean(result_metrics)
    lower = np.percentile(result_metrics, ((1 - confidence) / 2) * 100)
    upper = np.percentile(result_metrics, (confidence + ((1 - confidence) / 2)) * 100)
    return mean_val, lower, upper


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="")
    parser.add_argument("-m", '--model_path', type=str, required=True,
                        help="Path of the model to be evaluated")
    parser.add_argument("-g", '--groundtruth_tsv', type=str, required=True,
                        help="Path of the groundtruth tsv file")

    # Not required after that, but recommended to defined
    parser.add_argument("-mw", "--median_window", type=int, default=None,
                        help="Nb of frames for the median window, "
                             "if None the one defined for testing after training is used")

    # Next groundtruth variable could be ommited if same organization than DESED dataset
    parser.add_argument('--meta_gt', type=str, default=None,
                        help="Path of the groundtruth description of feat_filenames and durations")
    parser.add_argument("-ga", '--groundtruth_audio_dir', type=str, default=None,
                        help="Path of the groundtruth filename, (see in config, at dataset folder)")
    parser.add_argument("-s", '--save_predictions_path', type=str, default=None,
                        help="Path for the predictions to be saved (if needed)")

    parser.add_argument("-b", '--bootstrap_iterations', type=int, default=200,
                        help="Number of bootstrap samples to take (default 200, 80% taken each iteration).")
    # Dev
    parser.add_argument("-n", '--nb_files', type=int, default=None,
                        help="Number of files to be used. Useful when testing on small number of files.")
    f_args = parser.parse_args()

    # Get variables from f_args
    model_path, median_window, gt_audio_dir, groundtruth, durations = get_variables(f_args)

    # Model
    expe_state = torch.load(model_path, map_location="cpu")
    dataset = DESED(base_feature_dir=osp.join(cfg.workspace, "dataset", "features"), compute_log=False)

    gt_df_feat = dataset.initialize_and_get_df(f_args.groundtruth_tsv, gt_audio_dir, nb_files=f_args.nb_files)
    params = _load_state_vars(expe_state, gt_df_feat, median_window)

    # Preds with only one value
    single_predictions = get_predictions(params["model"], params["dataloader"],
                                         params["many_hot_encoder"].decode_strong, params["pooling_time_ratio"],
                                         median_window=params["median_window"],
                                         save_predictions=f_args.save_predictions_path)

    def macro_f1_func(predictions, gtruth_df, verbose=False):
        events_metric = compute_sed_eval_metrics(predictions, gtruth_df, verbose=verbose)
        macro_f1_event = events_metric.results_class_wise_average_metrics()['f_measure']['f_measure']
        return macro_f1_event
    
    mean_f1, lf1, uf1 = bootstrap(single_predictions, groundtruth, macro_f1_func,
                                  n_iterations=f_args.bootstrap_iterations)
    logger.info(f"f1 score: {mean_f1} -{lf1} +{uf1}")
    # f1_macro = compute_metrics(single_predictions, groundtruth, durations)

    # ##########
    # Optional but recommended
    # ##########
    # Compute psds scores with multiple thresholds (more accurate). n_thresholds could be increased.
    n_thresholds = 50
    # Example of 5 thresholds: 0.1, 0.3, 0.5, 0.7, 0.9
    list_thresholds = np.arange(1 / (n_thresholds * 2), 1, 1 / n_thresholds)
    pred_thresh = get_predictions(params["model"], params["dataloader"],
                                  params["many_hot_encoder"].decode_strong, params["pooling_time_ratio"],
                                  thresholds=list_thresholds, median_window=params["median_window"],
                                  save_predictions=f_args.save_predictions_path)
    # psds = compute_psds_from_operating_points(pred_thresh, groundtruth, durations)
    # fname_roc = None
    # if f_args.save_predictions_path is not None:
    #     fname_roc = osp.splitext(f_args.save_predictions_path)[0] + "_roc.png"
    # psds_ct = psds_score(psds, filename_roc_curves=fname_roc)

    def get_psds_score(pred_thresh, groundtruth, durations, verbose=False):
        psds = compute_psds_from_operating_points(pred_thresh, groundtruth, durations)
        psds_ct = psds_score(psds, verbose=verbose).value
        return psds_ct
    mean_psds, lpsds, upsds = bootstrap(pred_thresh, groundtruth, get_psds_score, durations=durations,
                                        n_iterations=f_args.bootstrap_iterations)
    logger.info(f"f1 score: {mean_psds} -{lpsds} +{upsds}")
