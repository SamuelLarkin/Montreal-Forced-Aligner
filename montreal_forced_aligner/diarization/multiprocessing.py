"""Multiprocessing functionality for speaker diarization"""
from __future__ import annotations

import logging
import os
import queue
import sys
import threading
import time
import typing
from pathlib import Path
from queue import Queue

import dataclassy

try:
    import hdbscan
    import kneed

    HDBSCAN_ENABLED = True
except ImportError:
    HDBSCAN_ENABLED = False
import librosa
import numpy as np
import sqlalchemy
from _kalpy.ivector import Plda, ivector_normalize_length, ivector_subtract_mean
from _kalpy.matrix import FloatVector
from kalpy.ivector.data import IvectorArchive
from scipy.spatial import distance
from sklearn import cluster, manifold, metrics, neighbors, preprocessing
from sqlalchemy.orm import joinedload

from montreal_forced_aligner.abc import KaldiFunction
from montreal_forced_aligner.config import GLOBAL_CONFIG
from montreal_forced_aligner.data import (
    ClusterType,
    DistanceMetric,
    ManifoldAlgorithm,
    MfaArguments,
)
from montreal_forced_aligner.db import File, Job, SoundFile, Speaker, Utterance

try:
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        torch_logger = logging.getLogger("speechbrain.utils.torch_audio_backend")
        torch_logger.setLevel(logging.ERROR)
        torch_logger = logging.getLogger("speechbrain.utils.train_logger")
        torch_logger.setLevel(logging.ERROR)
        import torch
        from speechbrain.pretrained import EncoderClassifier, SpeakerRecognition
    FOUND_SPEECHBRAIN = True
except (ImportError, OSError):
    FOUND_SPEECHBRAIN = False
    EncoderClassifier = None
    SpeakerRecognition = None

__all__ = [
    "PldaClassificationArguments",
    "PldaClassificationFunction",
    "ComputeEerArguments",
    "ComputeEerFunction",
    "SpeechbrainArguments",
    "SpeechbrainClassificationFunction",
    "SpeechbrainEmbeddingFunction",
    "cluster_matrix",
    "visualize_clusters",
]

logger = logging.getLogger("mfa")


# noinspection PyUnresolvedReferences
@dataclassy.dataclass(slots=True)
class PldaClassificationArguments(MfaArguments):
    """Arguments for :class:`~montreal_forced_aligner.diarization.multiprocessing.PldaClassificationFunction`"""

    plda: Plda
    train_ivector_path: Path
    num_utts_path: Path
    use_xvector: bool


# noinspection PyUnresolvedReferences
@dataclassy.dataclass(slots=True)
class ComputeEerArguments(MfaArguments):
    """Arguments for :class:`~montreal_forced_aligner.diarization.multiprocessing.ComputeEerFunction`"""

    plda: Plda
    metric: DistanceMetric
    use_xvector: bool
    limit_within_speaker: int
    limit_per_speaker: int


# noinspection PyUnresolvedReferences
@dataclassy.dataclass(slots=True)
class SpeechbrainArguments(MfaArguments):
    """Arguments for :class:`~montreal_forced_aligner.diarization.multiprocessing.SpeechbrainClassificationFunction`"""

    cuda: bool
    cluster: bool


def visualize_clusters(
    ivectors: np.ndarray,
    manifold_algorithm: ManifoldAlgorithm,
    metric_type: DistanceMetric,
    n_neighbors: int = 10,
    plda: typing.Optional[Plda] = None,
    quick=False,
):
    logger.debug(f"Generating 2D representation of ivectors with {manifold_algorithm.name}...")
    begin = time.time()
    to_fit = ivectors
    metric = metric_type.name
    tsne_angle = 0.5
    tsne_iterations = 1000
    mds_iterations = 300
    if quick:
        tsne_angle = 0.8
        tsne_iterations = 500
        mds_iterations = 150
    if metric_type is DistanceMetric.plda:
        logger.info("Generating precomputed distance matrix...")
        to_fit = metrics.pairwise_distances(
            ivectors,
            ivectors,
            metric=lambda x, y: plda.LogLikelihoodRatio(x, 1, y),
            n_jobs=GLOBAL_CONFIG.current_profile.num_jobs,
        )
        np.fill_diagonal(to_fit, 0)
        metric = "precomputed"
    if manifold_algorithm is ManifoldAlgorithm.mds:
        if metric_type is DistanceMetric.cosine:
            to_fit = preprocessing.normalize(ivectors, norm="l2")
            metric = "euclidean"
        points = manifold.MDS(
            dissimilarity=metric,
            random_state=0,
            n_jobs=GLOBAL_CONFIG.current_profile.num_jobs,
            max_iter=mds_iterations,
            metric=False,
            normalized_stress=True,
        ).fit_transform(to_fit)
    elif manifold_algorithm is ManifoldAlgorithm.tsne:
        points = manifold.TSNE(
            metric=metric,
            random_state=0,
            perplexity=n_neighbors,
            init="pca" if metric != "precomputed" else "random",
            n_jobs=GLOBAL_CONFIG.current_profile.num_jobs,
            angle=tsne_angle,
            n_iter=tsne_iterations,
        ).fit_transform(to_fit)
    elif manifold_algorithm is ManifoldAlgorithm.spectral:
        points = manifold.SpectralEmbedding(
            affinity="nearest_neighbors",
            random_state=0,
            n_neighbors=n_neighbors,
            n_jobs=GLOBAL_CONFIG.current_profile.num_jobs,
        ).fit_transform(to_fit)
    elif manifold_algorithm is ManifoldAlgorithm.isomap:
        points = manifold.Isomap(
            metric=metric, n_neighbors=n_neighbors, n_jobs=GLOBAL_CONFIG.current_profile.num_jobs
        ).fit_transform(to_fit)
    else:
        raise NotImplementedError
    logger.debug(f"Generating 2D representation took {time.time() - begin:.3f} seconds")
    return points


def calculate_distance_threshold(
    metric: typing.Union[str, callable],
    to_fit: np.ndarray,
    min_samples: int = 5,
    working_directory: str = None,
    score_metric_params=None,
    no_visuals: bool = False,
) -> float:
    """
    Calculate a threshold for the given ivectors using a relative threshold

    Parameters
    ----------
    metric: str or callable
        Metric to evaluate
    to_fit: numpy.ndarray
        Ivectors or distance matrix
    relative_distance_threshold: float
       Relative threshold from 0 to 1

    Returns
    -------
    float
        Absolute distance threshold
    """
    logger.debug(f"Calculating distance threshold from {min_samples} nearest neighbors...")
    nbrs = neighbors.NearestNeighbors(
        n_neighbors=min_samples,
        metric=metric,
        metric_params=score_metric_params,
        n_jobs=GLOBAL_CONFIG.current_profile.num_jobs,
    ).fit(to_fit)
    distances, indices = nbrs.kneighbors(to_fit)
    distances = distances[:, min_samples - 1]
    distances = np.sort(distances, axis=0)
    kneedle = kneed.KneeLocator(np.arange(distances.shape[0]), distances, curve="concave", S=5)
    index = kneedle.elbow
    threshold = distances[index]

    min_distance = np.min(distances)
    max_distance = np.max(distances)
    logger.debug(
        f"Distance threshold was set to {threshold} (range = {min_distance:.4f} - {max_distance:.4f})"
    )
    if GLOBAL_CONFIG.current_profile.debug and not no_visuals:
        import seaborn as sns
        from matplotlib import pyplot as plt

        sns.set()
        plt.plot(distances)
        plt.xlabel("Index")
        plt.ylabel("Distance to NN")
        plt.axvline(index, c="k")
        plt.text(
            index, max_distance, "threshold", horizontalalignment="right", verticalalignment="top"
        )

        if working_directory is not None:
            plot_path = os.path.join(working_directory, "nearest_neighbor_distances.png")
            close_string = f"Closing k-distance plot, it has been saved to {plot_path}."
            plt.savefig(plot_path, transparent=True)
        else:
            close_string = "Closing k-distance plot."
        if GLOBAL_CONFIG.current_profile.verbose:
            plt.show(block=False)
            plt.pause(10)
            logger.debug(close_string)
            plt.close()
    return float(threshold)


def cluster_matrix(
    ivectors: np.ndarray,
    cluster_type: ClusterType,
    metric: DistanceMetric = DistanceMetric.euclidean,
    strict=True,
    no_visuals=False,
    working_directory=None,
    **kwargs,
) -> np.ndarray:
    """
    Wrapper function for sklearn's clustering methods

    Parameters
    ----------
    ivectors: numpy.ndarray
        Ivectors to cluster
    cluster_type: :class:`~montreal_forced_aligner.data.ClusterType`
        Clustering algorithm
    metric: :class:`~montreal_forced_aligner.data.DistanceMetric`
        Distance metric to use in clustering
    strict: bool
        Flag for whether to raise exceptions when only one cluster is found
    kwargs
        Extra keyword arguments to pass to sklearn cluster classes

    Returns
    -------
    numpy.ndarray
        Cluster labels for each utterance
    """
    from montreal_forced_aligner.config import GLOBAL_CONFIG

    logger.debug(f"Running {cluster_type}...")

    if sys.platform == "win32" and cluster_type is ClusterType.kmeans:
        os.environ["OMP_NUM_THREADS"] = "1"
        os.environ["OPENBLAS_NUM_THREADS"] = "1"
        os.environ["MKL_NUM_THREADS"] = "1"
    else:
        os.environ["OMP_NUM_THREADS"] = f"{GLOBAL_CONFIG.current_profile.num_jobs}"
        os.environ["OPENBLAS_NUM_THREADS"] = f"{GLOBAL_CONFIG.current_profile.num_jobs}"
        os.environ["MKL_NUM_THREADS"] = f"{GLOBAL_CONFIG.current_profile.num_jobs}"
    distance_threshold = kwargs.pop("distance_threshold", None)
    plda: Plda = kwargs.pop("plda", None)
    min_cluster_size = kwargs.pop("min_cluster_size", 15)

    score_metric = metric.value
    to_fit = ivectors
    score_metric_params = None
    if score_metric == "plda" and cluster_type is not ClusterType.affinity:
        logger.debug("Generating precomputed distance matrix...")
        begin = time.time()

        to_fit = plda.generate_affinity_matrix(to_fit).numpy()
        logger.debug(f"Precomputed distance matrix took {time.time() - begin:.3f} seconds")
        score_metric = "precomputed"
    if cluster_type is ClusterType.affinity:
        affinity = metric
        if metric is DistanceMetric.cosine:
            to_fit = preprocessing.normalize(to_fit, norm="l2")
            score_metric = "euclidean"
            affinity = "euclidean"
        elif metric is DistanceMetric.plda:
            logger.debug("Generating precomputed distance matrix...")
            to_fit = metrics.pairwise_distances(
                to_fit,
                to_fit,
                metric=plda.log_likelihood,
                n_jobs=GLOBAL_CONFIG.current_profile.num_jobs,
            )

            score_metric = "precomputed"
            affinity = "precomputed"
        c_labels = cluster.AffinityPropagation(
            affinity=affinity,
            copy=False,
            random_state=GLOBAL_CONFIG.current_profile.seed,
            verbose=GLOBAL_CONFIG.current_profile.verbose,
            **kwargs,
        ).fit_predict(to_fit)
    elif cluster_type is ClusterType.agglomerative:
        if metric is DistanceMetric.cosine:
            to_fit = preprocessing.normalize(to_fit, norm="l2")
            score_metric = "euclidean"
        if not kwargs["n_clusters"]:
            if distance_threshold is not None:
                eps = distance_threshold
            else:
                eps = calculate_distance_threshold(
                    score_metric,
                    to_fit,
                    min_cluster_size,
                    working_directory,
                    score_metric_params=score_metric_params,
                    no_visuals=no_visuals,
                )
            kwargs["distance_threshold"] = eps
        c_labels = cluster.AgglomerativeClustering(metric=score_metric, **kwargs).fit_predict(
            to_fit
        )
    elif cluster_type is ClusterType.spectral:
        affinity = "nearest_neighbors"
        if metric is DistanceMetric.cosine:
            to_fit = preprocessing.normalize(to_fit, norm="l2")
            score_metric = "euclidean"
        elif metric is DistanceMetric.plda:
            logger.info("Generating precomputed distance matrix...")
            affinity = "precomputed_nearest_neighbors"
            to_fit = metrics.pairwise_distances(
                to_fit, to_fit, metric=score_metric, n_jobs=GLOBAL_CONFIG.current_profile.num_jobs
            )
            np.fill_diagonal(to_fit, 0)
            score_metric = "precomputed"
        c_labels = cluster.SpectralClustering(
            affinity=affinity,
            n_jobs=GLOBAL_CONFIG.current_profile.num_jobs,
            random_state=GLOBAL_CONFIG.current_profile.seed,
            verbose=GLOBAL_CONFIG.current_profile.verbose,
            **kwargs,
        ).fit_predict(to_fit)
    elif cluster_type is ClusterType.dbscan:
        if distance_threshold is not None:
            eps = distance_threshold
        else:
            eps = calculate_distance_threshold(
                score_metric,
                to_fit,
                min_cluster_size,
                working_directory,
                score_metric_params=score_metric_params,
                no_visuals=no_visuals,
            )
        c_labels = cluster.DBSCAN(
            min_samples=min_cluster_size,
            metric=score_metric,
            eps=eps,
            n_jobs=GLOBAL_CONFIG.current_profile.num_jobs,
            **kwargs,
        ).fit_predict(to_fit)
    elif cluster_type is ClusterType.meanshift:
        if score_metric == "cosine":
            to_fit = preprocessing.normalize(to_fit, norm="l2")
            score_metric = "euclidean"
        c_labels = cluster.MeanShift(
            n_jobs=GLOBAL_CONFIG.current_profile.num_jobs, **kwargs
        ).fit_predict(to_fit)
    elif cluster_type is ClusterType.hdbscan:
        if not HDBSCAN_ENABLED:
            raise ImportError("Please install `hdbscan` package.")
        if score_metric == "cosine":
            to_fit = preprocessing.normalize(to_fit, norm="l2")
            score_metric = "euclidean"
        min_samples = max(5, int(min_cluster_size / 4))
        if distance_threshold is not None:
            eps = distance_threshold
        else:
            eps = calculate_distance_threshold(
                score_metric,
                to_fit,
                min_cluster_size,
                working_directory,
                score_metric_params=score_metric_params,
                no_visuals=no_visuals,
            )
        if score_metric == "precomputed" or metric is DistanceMetric.plda:
            algorithm = "best"
        else:
            algorithm = "boruvka_balltree"
        c_labels = hdbscan.HDBSCAN(
            min_samples=min_samples,
            min_cluster_size=min_cluster_size,
            cluster_selection_epsilon=eps,
            metric=score_metric,
            algorithm=algorithm,
            core_dist_n_jobs=GLOBAL_CONFIG.current_profile.num_jobs,
            **kwargs,
        ).fit_predict(to_fit)
    elif cluster_type is ClusterType.optics:
        if distance_threshold is not None:
            eps = distance_threshold
        else:
            eps = calculate_distance_threshold(
                score_metric,
                to_fit,
                min_cluster_size,
                working_directory,
                score_metric_params=score_metric_params,
                no_visuals=no_visuals,
            )
        c_labels = cluster.OPTICS(
            min_samples=min_cluster_size,
            max_eps=eps,
            metric=score_metric,
            n_jobs=GLOBAL_CONFIG.current_profile.num_jobs,
            **kwargs,
        ).fit_predict(to_fit)
    elif cluster_type is ClusterType.kmeans:
        if score_metric == "cosine":
            to_fit = preprocessing.normalize(to_fit, norm="l2")
            score_metric = "euclidean"
        c_labels = cluster.MiniBatchKMeans(
            verbose=GLOBAL_CONFIG.current_profile.verbose, n_init="auto", **kwargs
        ).fit_predict(to_fit)
    else:
        raise NotImplementedError(f"The cluster type '{cluster_type}' is not supported.")
    num_clusters = np.unique(c_labels).shape[0]
    logger.debug(f"Found {num_clusters} clusters")
    try:
        if score_metric == "plda":
            score_metric = plda.distance
        elif score_metric == "precomputed":
            if cluster_type is ClusterType.affinity:
                to_fit = np.max(to_fit) - to_fit
            np.fill_diagonal(to_fit, 0)
        score = metrics.silhouette_score(to_fit, c_labels, metric=score_metric)
        logger.debug(f"Silhouette score (-1-1): {score}")
    except ValueError:
        if num_clusters == 1:
            logger.warning(
                "Only found one cluster, please adjust cluster parameters to generate more clusters."
            )
            if strict:
                raise
    os.environ["OMP_NUM_THREADS"] = f"{GLOBAL_CONFIG.current_profile.blas_num_threads}"
    os.environ["OPENBLAS_NUM_THREADS"] = f"{GLOBAL_CONFIG.current_profile.blas_num_threads}"
    os.environ["MKL_NUM_THREADS"] = f"{GLOBAL_CONFIG.current_profile.blas_num_threads}"

    return c_labels


class PldaClassificationFunction(KaldiFunction):
    """
    Multiprocessing function to compute voice activity detection

    See Also
    --------
    :meth:`.AcousticCorpusMixin.compute_vad`
        Main function that calls this function in parallel
    :meth:`.AcousticCorpusMixin.compute_vad_arguments`
        Job method for generating arguments for this function
    :kaldi_src:`compute-vad`
        Relevant Kaldi binary

    Parameters
    ----------
    args: :class:`~montreal_forced_aligner.corpus.features.VadArguments`
        Arguments for the function
    """

    def __init__(self, args: PldaClassificationArguments):
        super().__init__(args)
        self.plda = args.plda
        self.train_ivector_path = args.train_ivector_path
        self.num_utts_path = args.num_utts_path
        self.use_xvector = args.use_xvector

    def _run(self):
        """Run the function"""

        ivector_archive = IvectorArchive(
            self.train_ivector_path, num_utterances_file_name=self.num_utts_path
        )
        speaker_ivectors = []
        speaker_ids = []
        num_utts = []
        for speaker_id, ivector, utts in ivector_archive:
            speaker_ids.append(speaker_id)
            num_utts.append(utts)
            ivector_normalize_length(ivector)
            speaker_ivectors.append(FloatVector(ivector))
        ivector_subtract_mean(speaker_ivectors)
        speaker_ivectors = self.plda.transform_ivectors(speaker_ivectors, num_utts)
        with self.session() as session:

            job: Job = (
                session.query(Job)
                .options(joinedload(Job.corpus, innerjoin=True))
                .filter(Job.id == self.job_name)
                .first()
            )
            utterances = (
                session.query(Utterance.id, Utterance.plda_vector)
                .filter(Utterance.plda_vector != None)  # noqa
                .filter(Utterance.job_id == job.id)
                .order_by(Utterance.kaldi_id)
            )
            for u_id, u_ivector in utterances:
                ivector = FloatVector()
                ivector.from_numpy(u_ivector)
                ind, score = self.plda.classify_utterance(ivector, speaker_ivectors, num_utts)
                speaker = speaker_ids[ind]
                self.callback((u_id, speaker, score))


class ComputeEerFunction(KaldiFunction):
    """
    Multiprocessing function to compute voice activity detection

    See Also
    --------
    :meth:`.AcousticCorpusMixin.compute_vad`
        Main function that calls this function in parallel
    :meth:`.AcousticCorpusMixin.compute_vad_arguments`
        Job method for generating arguments for this function
    :kaldi_src:`compute-vad`
        Relevant Kaldi binary

    Parameters
    ----------
    args: :class:`~montreal_forced_aligner.corpus.features.VadArguments`
        Arguments for the function
    """

    def __init__(self, args: ComputeEerArguments):
        super().__init__(args)
        self.plda = args.plda
        self.metric = args.metric
        self.use_xvector = args.use_xvector
        self.limit_within_speaker = args.limit_within_speaker
        self.limit_per_speaker = args.limit_per_speaker

    # noinspection PyTypeChecker
    def _run(self):
        """Run the function"""
        if self.use_xvector:
            columns = [Utterance.id, Utterance.speaker_id, Utterance.xvector]
            filter = Utterance.xvector != None  # noqa
        else:
            columns = [Utterance.id, Utterance.speaker_id, Utterance.plda_vector]
            filter = Utterance.plda_vector != None  # noqa
        with self.session() as session:
            speakers = (
                session.query(Speaker.id)
                .join(Speaker.utterances)
                .filter(Utterance.job_id == self.job_name)
                .order_by(Speaker.id)
                .distinct(Speaker.id)
            )
            for (s_id,) in speakers:
                match_scores = []
                mismatch_scores = []
                random_within_speaker = (
                    session.query(*columns)
                    .filter(filter, Utterance.speaker_id == s_id)
                    .order_by(sqlalchemy.func.random())
                    .limit(self.limit_within_speaker)
                )
                for u_id, s_id, u_ivector in random_within_speaker:
                    comp_query = (
                        session.query(columns[2])
                        .filter(filter, Utterance.speaker_id == s_id, Utterance.id != u_id)
                        .order_by(sqlalchemy.func.random())
                        .limit(self.limit_within_speaker)
                    )
                    for (u2_ivector,) in comp_query:
                        if self.metric is DistanceMetric.plda:
                            score = self.plda.distance(u_ivector, u2_ivector)
                        elif self.metric is DistanceMetric.cosine:
                            score = distance.cosine(u_ivector, u2_ivector)
                        else:
                            score = distance.euclidean(u_ivector, u2_ivector)
                        match_scores.append(score)
                other_speakers = session.query(Speaker.id).filter(Speaker.id != s_id)
                for (o_s_id,) in other_speakers:
                    random_out_speaker = (
                        session.query(columns[2])
                        .filter(filter, Utterance.speaker_id == s_id)
                        .order_by(sqlalchemy.func.random())
                        .limit(self.limit_per_speaker)
                    )
                    for (u_ivector,) in random_out_speaker:
                        comp_query = (
                            session.query(columns[2])
                            .filter(filter, Utterance.speaker_id == o_s_id)
                            .order_by(sqlalchemy.func.random())
                            .limit(self.limit_per_speaker)
                        )
                        for (u2_ivector,) in comp_query:
                            if self.metric is DistanceMetric.plda:
                                score = self.plda.distance(u_ivector, u2_ivector)
                            elif self.metric is DistanceMetric.cosine:
                                score = distance.cosine(u_ivector, u2_ivector)
                            else:
                                score = distance.euclidean(u_ivector, u2_ivector)
                            mismatch_scores.append(score)
                self.callback((match_scores, mismatch_scores))


class SpeechbrainClassificationFunction(KaldiFunction):
    """
    Multiprocessing function to classify speakers based on a speechbrain model

    Parameters
    ----------
    args: :class:`~montreal_forced_aligner.diarization.multiprocessing.SpeechbrainArguments`
        Arguments for the function
    """

    def __init__(self, args: SpeechbrainArguments):
        super().__init__(args)
        self.cuda = args.cuda
        self.cluster = args.cluster

    def _run(self) -> typing.Generator[typing.Tuple[int, int, int]]:
        """Run the function"""
        run_opts = None
        if self.cuda:
            run_opts = {"device": "cuda"}
        model = EncoderClassifier.from_hparams(
            source="speechbrain/spkrec-ecapa-voxceleb",
            savedir=os.path.join(
                GLOBAL_CONFIG.current_profile.temporary_directory,
                "models",
                "SpeakerRecognition",
            ),
            run_opts=run_opts,
        )
        device = torch.device("cuda" if self.cuda else "cpu")
        with self.session() as session:

            job: Job = (
                session.query(Job)
                .options(joinedload(Job.corpus, innerjoin=True))
                .filter(Job.id == self.job_name)
                .first()
            )
            utterances = session.query(Utterance.id, Utterance.xvector).filter(
                Utterance.xvector != None, Utterance.job_id == job.id  # noqa
            )
            for u_id, ivector in utterances:
                ivector = torch.tensor(ivector, device=device).unsqueeze(0).unsqueeze(0)
                out_prob = model.mods.classifier(ivector).squeeze(1)
                score, index = torch.max(out_prob, dim=-1)
                text_lab = model.hparams.label_encoder.decode_torch(index)
                new_speaker = text_lab[0]
                del out_prob
                del index
                self.callback((u_id, new_speaker, float(score.cpu().numpy())))
                del text_lab
                del new_speaker
                del score
                if self.cuda:
                    torch.cuda.empty_cache()
        del model
        if self.cuda:
            torch.cuda.empty_cache()


class SpeechbrainEmbeddingFunction(KaldiFunction):
    """
    Multiprocessing function to generating xvector embeddings from a speechbrain model

    Parameters
    ----------
    args: :class:`~montreal_forced_aligner.diarization.multiprocessing.SpeechbrainArguments`
        Arguments for the function
    """

    def __init__(self, args: SpeechbrainArguments):
        super().__init__(args)
        self.cuda = args.cuda
        self.cluster = args.cluster

    def _run(self) -> typing.Generator[typing.Tuple[int, int, int]]:
        """Run the function"""
        run_opts = None
        if self.cuda:
            run_opts = {"device": "cuda"}
        if self.cluster:
            model_class = SpeakerRecognition
        else:
            model_class = EncoderClassifier

        model = model_class.from_hparams(
            source="speechbrain/spkrec-ecapa-voxceleb",
            savedir=os.path.join(
                GLOBAL_CONFIG.current_profile.temporary_directory,
                "models",
                "SpeakerRecognition",
            ),
            run_opts=run_opts,
        )

        return_q = Queue(2)
        finished_adding = threading.Event()
        stopped = threading.Event()
        loader = UtteranceFileLoader(
            self.job_name, self.session, return_q, stopped, finished_adding
        )
        loader.start()
        exception = None
        device = torch.device("cuda" if self.cuda else "cpu")
        while True:
            try:
                result = return_q.get(timeout=1)
            except queue.Empty:
                if finished_adding.is_set():
                    break
                continue
            if stopped.is_set():
                continue
            if isinstance(result, Exception):
                stopped.set()
                continue

            u_id, y = result
            emb = (
                model.encode_batch(
                    torch.tensor(y[np.newaxis, :], device=device), normalize=self.cluster
                )
                .cpu()
                .numpy()
                .squeeze(axis=1)
            )
            self.callback((u_id, emb[0]))
            del emb
            if self.cuda:
                torch.cuda.empty_cache()

        loader.join()
        if exception:
            raise Exception


class UtteranceFileLoader(threading.Thread):
    """
    Helper process for loading utterance waveforms in parallel with embedding extraction

    Parameters
    ----------
    job_name: int
        Job identifier
    db_string: str
        Connection string for database
    return_q: multiprocessing.Queue
        Queue to put waveforms
    stopped: :class:`~threading.Event`
        Check for whether the process to exit gracefully
    finished_adding: :class:`~threading.Event`
        Check for whether the worker has processed all utterances
    """

    def __init__(
        self,
        job_name: int,
        session: sqlalchemy.orm.scoped_session,
        return_q: Queue,
        stopped: threading.Event,
        finished_adding: threading.Event,
    ):
        super().__init__()
        self.job_name = job_name
        self.session = session
        self.return_q = return_q
        self.stopped = stopped
        self.finished_adding = finished_adding

    def run(self) -> None:
        """
        Run the waveform loading job
        """
        with self.session() as session:
            try:
                utterances = (
                    session.query(
                        Utterance.id,
                        Utterance.begin,
                        Utterance.duration,
                        SoundFile.sound_file_path,
                    )
                    .join(Utterance.file)
                    .join(File.sound_file)
                    .filter(Utterance.job_id == self.job_name)
                )
                for u_id, begin, duration, sound_file_path in utterances:
                    if self.stopped.is_set():
                        break
                    y, _ = librosa.load(
                        sound_file_path,
                        sr=16000,
                        mono=False,
                        offset=begin,
                        duration=duration,
                    )
                    self.return_q.put((u_id, y))
            except Exception as e:
                self.return_q.put(e)
            finally:
                self.finished_adding.set()
