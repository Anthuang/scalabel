"""Evaluation procedures for image tagging."""
import argparse
import json
from itertools import chain
from typing import AbstractSet, Dict, List, Optional, Union

import numpy as np

from ..common.io import open_write_text
from ..common.logger import logger
from ..common.parallel import NPROC
from ..common.typing import NDArrayI32
from ..label.io import load, load_label_config
from ..label.typing import Config, Frame
from ..label.utils import get_parent_categories
from .result import AVERAGE, Result, Scores, ScoresList
from .utils import reorder_preds


def _unique_labels(y_true, y_pred):
    def __unique_labels(y):
        if hasattr(y, "__array__"):
            return np.unique(np.asarray(y))
        else:
            return set(y)

    ys_labels = set(
        chain.from_iterable(__unique_labels(y) for y in (y_true, y_pred))
    )
    return np.array(sorted(ys_labels))


def _count_nonzero(X, axis=None, sample_weight=None):
    """A variant of X.getnnz() with extension to weighting on axis 0
    Useful in efficiently calculating multilabel metrics.
    """
    if axis == -1:
        axis = 1
    elif axis == -2:
        axis = 0
    elif X.format != "csr":
        raise TypeError("Expected CSR sparse format, got {0}".format(X.format))

    # We rely here on the fact that np.diff(Y.indptr) for a CSR
    # will return the number of nonzero entries in each row.
    # A bincount over Y.indices will return the number of nonzeros
    # in each column. See ``csr_matrix.getnnz`` in scipy >= 0.14.
    if axis is None:
        return np.dot(np.diff(X.indptr), sample_weight)
    elif axis == 1:
        out = np.diff(X.indptr)
        return out * sample_weight
    elif axis == 0:
        weights = np.repeat(sample_weight, np.diff(X.indptr))
        return np.bincount(X.indices, minlength=X.shape[1], weights=weights)
    else:
        raise ValueError("Unsupported axis: {0}".format(axis))


def _multilabel_confusion_matrix(y_true, y_pred, labels):
    """Compute a confusion matrix for each class or sample."""
    present_labels = _unique_labels(y_true, y_pred)
    n_labels = len(labels)
    labels = np.hstack(
        [labels, np.setdiff1d(present_labels, labels, assume_unique=True)]
    )

    if n_labels is not None:
        y_true = y_true[:, labels[:n_labels]]
        y_pred = y_pred[:, labels[:n_labels]]

    # calculate weighted counts
    sum_axis = 0
    true_and_pred = y_true.multiply(y_pred)
    tp_sum = _count_nonzero(true_and_pred, axis=sum_axis)
    pred_sum = _count_nonzero(y_pred, axis=sum_axis)
    true_sum = _count_nonzero(y_true, axis=sum_axis)

    fp = pred_sum - tp_sum
    fn = true_sum - tp_sum
    tp = tp_sum

    tn = y_true.shape[0] - tp - fp - fn
    return np.array([tn, fp, fn, tp]).T.reshape(-1, 2, 2)


def _prf_divide(numerator, denominator):
    """Performs division and handles divide-by-zero."""
    mask = denominator == 0.0
    denominator = denominator.copy()
    denominator[mask] = 1  # avoid infs/nans
    result = numerator / denominator

    if not np.any(mask):
        return result

    # address zero division by setting 0s to 1s
    result[mask] = 1.0
    return result


def _precision_recall_fscore_support(
    y_true,
    y_pred,
    labels,
    beta=1.0,
):
    """Compute precision, recall, F-measure and support for each class."""
    mcm = _multilabel_confusion_matrix(
        y_true,
        y_pred,
        labels,
    )
    tp_sum = mcm[:, 1, 1]
    pred_sum = tp_sum + mcm[:, 0, 1]
    true_sum = tp_sum + mcm[:, 1, 0]

    beta2 = beta ** 2

    # divide and set scores
    precision = _prf_divide(tp_sum, pred_sum)
    recall = _prf_divide(tp_sum, true_sum)

    # if tp == 0 F will be 1 only if all predictions are zero, all labels are
    # zero, and zero_division=1. In all other case, 0
    if np.isposinf(beta):
        f_score = recall
    else:
        denom = beta2 * precision + recall

        denom[denom == 0.0] = 1  # avoid division by 0
        f_score = (1 + beta2) * precision * recall / denom

    weights = None

    return precision, recall, f_score, true_sum


def compute_scores(
    y_true,
    y_pred,
    target_names,
):
    """Build a text report showing the main classification metrics."""
    labels = _unique_labels(y_true, y_pred)

    if target_names and len(labels) != len(target_names):
        raise ValueError(
            "Number of classes, {0}, does not match size of "
            "target_names, {1}. Try specifying the labels "
            "parameter".format(len(labels), len(target_names))
        )

    headers = ["precision", "recall", "f1-score", "support"]
    # compute per-class results without averaging
    p, r, f1, s = _precision_recall_fscore_support(
        y_true,
        y_pred,
        labels,
    )
    rows = zip(target_names, p, r, f1, s)

    report_dict = {label[0]: label[1:] for label in rows}
    for label, scores in report_dict.items():
        report_dict[label] = dict(zip(headers, [i.item() for i in scores]))

    return report_dict


class TaggingResult(Result):
    """The class for general image tagging evaluation results."""

    precision: List[Dict[str, float]]
    recall: List[Dict[str, float]]
    f1_score: List[Dict[str, float]]
    accuracy: List[Dict[str, float]]

    def __eq__(self, other: "TaggingResult") -> bool:  # type: ignore
        """Check whether two instances are equal."""
        return super().__eq__(other)

    def summary(
        self,
        include: Optional[AbstractSet[str]] = None,
        exclude: Optional[AbstractSet[str]] = None,
    ) -> Scores:
        """Convert tagging results into a flattened dict as the summary."""
        summary_dict: Dict[str, Union[int, float]] = {}
        for metric, scores_list in self.dict(include=include, exclude=exclude).items():  # type: ignore
            for category, score in scores_list[-2].items():
                summary_dict[f"{metric}/{category}"] = score
            summary_dict[metric] = scores_list[-1][AVERAGE]
        return summary_dict


def evaluate_tagging(
    ann_frames: List[Frame],
    pred_frames: List[Frame],
    config: Config,
    nproc: int = NPROC,  # pylint: disable=unused-argument
) -> TaggingResult:
    """Evaluate image tagging with Scalabel format.

    Args:
        ann_frames: the ground truth frames.
        pred_frames: the prediction frames.
        config: Metadata config.
        nproc: the number of process.

    Returns:
        TaggingResult: evaluation results.
    """
    pred_frames = reorder_preds(ann_frames, pred_frames)
    tag_classes = get_parent_categories(config.categories)
    assert tag_classes, "Tag attributes must be specified as supercategories"
    metrics = ["precision", "recall", "f1_score", "accuracy"]
    outputs: Dict[str, ScoresList] = {m: [] for m in metrics}
    avgs: Dict[str, Scores] = {m: {} for m in metrics}
    for tag, class_list in tag_classes.items():
        classes = [c.name for c in class_list]
        preds_cls, gts_cls = [], []
        for p, g in zip(pred_frames, ann_frames):
            if g.attributes is None:
                continue
            assert p.attributes is not None
            p_attr, g_attr = p.attributes[tag], g.attributes[tag]
            assert isinstance(p_attr, str) and isinstance(g_attr, str)
            assert p_attr in classes and g_attr in classes
            preds_cls.append(classes.index(p_attr))
            gts_cls.append(classes.index(g_attr))
        parray: NDArrayI32 = np.array(preds_cls, dtype=np.int32)
        garray: NDArrayI32 = np.array(gts_cls, dtype=np.int32)
        gt_classes = [classes[cid] for cid in sorted(set(gts_cls + preds_cls))]
        scores = compute_scores(garray, parray, gt_classes)
        out: Dict[str, Scores] = {}
        for metric in ["precision", "recall", "f1-score"]:
            met = metric if metric != "f1-score" else "f1_score"
            out[met] = {}
            for cat in classes:
                out[met][f"{tag}.{cat}"] = (
                    scores[cat][metric] * 100.0 if cat in scores else np.nan
                )
            avgs[met][tag.upper()] = (
                scores["macro avg"][metric] * 100.0
                if len(scores) > 3
                else np.nan
            )
        out["accuracy"] = {f"{tag}.{cat}": np.nan for cat in classes}
        avgs["accuracy"][tag.upper()] = scores["accuracy"] * 100.0
        for m, v in out.items():
            outputs[m].append(v)
    for m, v in avgs.items():
        outputs[m].append(v)
        outputs[m].append({AVERAGE: np.nanmean(list(v.values()))})
    return TaggingResult(**outputs)


def parse_arguments() -> argparse.Namespace:
    """Parse the arguments."""
    parser = argparse.ArgumentParser(description="Tagging evaluation.")
    parser.add_argument(
        "--gt", "-g", required=True, help="path to tagging ground truth"
    )
    parser.add_argument(
        "--result", "-r", required=True, help="path to tagging results"
    )
    parser.add_argument(
        "--config",
        "-c",
        default=None,
        help="Path to config toml file. Contains definition of categories, "
        "and optionally attributes and resolution. For an example "
        "see scalabel/label/testcases/configs.toml",
    )
    parser.add_argument(
        "--out-file",
        default="",
        help="Output file for tagging evaluation results.",
    )
    parser.add_argument(
        "--nproc",
        "-p",
        type=int,
        default=NPROC,
        help="number of processes for tagging evaluation",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_arguments()
    dataset = load(args.gt, args.nproc)
    gts, cfg = dataset.frames, dataset.config
    preds = load(args.result).frames
    if args.config is not None:
        cfg = load_label_config(args.config)
    if cfg is None:
        raise ValueError(
            "Dataset config is not specified. Please use --config"
            " to specify a config for this dataset."
        )
    eval_result = evaluate_tagging(gts, preds, cfg, args.nproc)
    logger.info(eval_result)
    logger.info(eval_result.summary())
    if args.out_file:
        with open_write_text(args.out_file) as fp:
            json.dump(eval_result.json(), fp)
