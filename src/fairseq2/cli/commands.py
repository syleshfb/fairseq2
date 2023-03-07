"""
Main entry point for fairseq2
"""
import datetime
import hashlib
import itertools
import logging
import os
import random
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

import submitit
import torch
import torchtnt.framework as tnt

import fairseq2.callbacks
import fairseq2.distributed
from fairseq2.cli import XP, DynamicModule
from fairseq2.tasks import Seq2Seq

logging.basicConfig(level=logging.INFO)
# TODO: train/evaluate should also setup logging to a specific experiment file
log = logging.getLogger("fairseq2.cli")


def sha_key(overrides: Iterable[str]) -> str:
    # TODO: breaking change, move this to XP, and include the script/config hash
    # TODO: could we use nice name like W&B instead of hexdigests ?
    return hashlib.sha256(";".join(overrides).encode("utf-8")).hexdigest()[:8]


def train(
    script: Path,
    workdir: Optional[Path] = None,
    partition: str = "debug",
    num_gpus: int = 1,
    eval_freq: int = -1,
    overrides: List[str] = [],
) -> None:
    """Launches a training script.

    script: the training script to launch. It needs at least:
        - a "task" function that returns a fairseq2 task (or a torchtnt train unit)
        - a "train_data" function that returns a dataloader for the task
        - a "valid_data" function if --eval_freq is set.

    workdir: we will create an XP dir there and put it the script and model snapshots.
    """
    slurm_args, overrides = _extract_slurm_args(overrides)
    if workdir is None:
        workdir = script.parent.resolve()
        if "/fairseq2/examples/" in str(script.resolve()):
            raise Exception(
                "We don't want to generate models inside the fairseq2 git repo. Specify a valid workdir with 'workdir=...'"
            )
    else:
        # Make a copy of script to workdir
        workdir = workdir.resolve()
        workdir.mkdir(exist_ok=True)
        xp_sha = sha_key(overrides)
        if workdir.name != xp_sha:
            workdir = workdir / xp_sha
            workdir.mkdir(exist_ok=True)
        workdir_script = workdir / script.name
        if workdir_script != script:
            workdir_script.write_bytes(script.read_bytes())
        script = workdir_script

    env = fairseq2.distributed.init(
        workdir, partition, num_gpus, one_file=False, slurm_args=slurm_args
    )
    xp = XP(script, script.with_suffix(".yaml"), overrides)
    entry_point = "train"

    # TODO: allow script to be a yaml file
    module = DynamicModule.from_script(script, overrides=overrides)
    _setup_module(module, env, xp, entry_point)

    # Dataloader may start subprocess.
    # Do this before having loaded the model
    # TODO: merge train_data and valid_data
    train_data = module.call_fn("train_data", caller=entry_point)
    eval_data = (
        module.call_fn("valid_data", caller=entry_point) if eval_freq > 0 else []
    )
    task = module.call_fn("task", caller=entry_point)

    train_state = tnt.init_fit_state(
        train_data,
        eval_data,
        evaluate_every_n_steps=eval_freq if eval_freq > 0 else None,
        evaluate_every_n_epochs=None,
    )

    module.serialize(xp.config_file)
    # Try to resume from the same workdir.
    # TODO: allow to restart from scratch, or to only reset optimizers
    fairseq2.callbacks.load_from_last_snapshot(str(workdir), train_state, task)

    callbacks = module.call_fn("callbacks", caller="train")
    tnt.fit(train_state, task, callbacks=callbacks)


def grid(
    script: Path,
    workdir: Path,
    partition: str,
    num_gpus: int = 1,
    eval_freq: int = -1,
    overrides: List[str] = [],
) -> None:
    """
    Launch multiple training on SLURM, using a grid-search over the given parameters.

    Use key=value1,value2 to iterate over several values for the argument 'key'.

    - workdir: Where to create exp folder. Each experiment will have its own sub-folder
    - partition: SLURM partition to use
    - num_gpus: Number of gpus to use for each run
    - eval_freq: Evaluation frequency
    """
    workdir.mkdir(exist_ok=True)
    workdir = workdir.resolve()
    slurm_args, overrides = _extract_slurm_args(overrides)
    fixed_overrides = []
    grid_overrides = []
    for override in overrides:
        if "," in override:
            assert (
                "=" in override
            ), f"Can't parse override: {override}. Missing '='. Expected syntax is: key=value1,value2"
            key, raw_values = override.split("=", 1)
            grid_overrides.append(
                ["=".join((key, val)) for val in raw_values.split(",")]
            )
        else:
            fixed_overrides.append(override)

    experiments = itertools.product(*grid_overrides)

    # Those can be overriden by passing 'slurm.timeout=600' or 'slurm.cpus_per_task=8'
    cpus_per_gpu = 5
    num_gpu_per_node = 8
    default_timeout = 3 * 24 * 60
    ex = submitit.AutoExecutor(
        folder=workdir / "logs", cluster="local" if partition == "local" else "slurm"
    )
    # Launch job with one task per gpu
    ex.update_parameters(
        name=script.stem,
        nodes=max(num_gpus // num_gpu_per_node, 1),
        gpus_per_node=min(num_gpus, num_gpu_per_node),
        tasks_per_node=min(num_gpus, num_gpu_per_node),
        cpus_per_task=cpus_per_gpu,
        timeout_min=default_timeout,
        slurm_partition=partition,
        slurm_additional_parameters=slurm_args,
    )

    jobs = []
    with ex.batch():
        for xp in experiments:
            # TODO: we should validate experiments **BEFORE** sending them to the queue.
            # Otherwise we might wait a long time just to realize we made a typo.
            xp_overrides = fixed_overrides + list(xp)
            # Copy the script inside its future workdir.
            # The job can take some time to get scheduled, having a copy of the script
            # ensures that we aren't modifying before the job actually starts.
            xp_sha = sha_key(xp_overrides)
            xp_workdir = workdir / xp_sha
            xp_workdir.mkdir(exist_ok=True)
            xp_script = xp_workdir / script.name
            xp_script.write_bytes(script.read_bytes())

            job = ex.submit(
                train,
                xp_script,
                workdir=xp_workdir,
                eval_freq=eval_freq,
                overrides=xp_overrides,
            )
            jobs.append(job)

    print(f"Launched {len(jobs)} in job array on partition {partition}. {jobs[0]}")
    for job in submitit.helpers.as_completed(jobs):
        print(job)
        if job.exception() is not None:
            print(job.exception())


def evaluate(
    snapshot_dir: Path,
    partition: str = "debug",
    num_gpus: int = 1,
    script: Optional[Path] = None,
    overrides: List[str] = [],
) -> None:
    """
    Loads a model from a snapshot dir and runs the corresponding evaluation.

    - snapshot_dir: the folder containing the model weights and hubconf.py
    - script: overrides the "hubconf.py" found in the model snapshot. This can have unexpected results.
    """
    import torchsnapshot

    if not snapshot_dir.exists():
        raise FileNotFoundError(f"Snapshot {snapshot_dir} not found.")
    script = script or snapshot_dir / "hubconf.py"
    if not script.exists():
        raise FileNotFoundError(f"{script} not found !")
    train_config = snapshot_dir / "hubconf.yaml"
    assert train_config.exists(), f"{train_config} not found !"

    slurm_args, overrides = _extract_slurm_args(overrides)
    xp_sha = "_" + sha_key(overrides) if overrides else ""
    # Create a different yaml file to store the eval config
    # This will mostly be the same than train config,
    # but it won't have trainer specific info, and might have some overrides
    xp = XP(script, snapshot_dir / f"evaluate{xp_sha}.yaml", overrides)

    env = fairseq2.distributed.init(
        snapshot_dir, partition, num_gpus, slurm_args=slurm_args
    )

    module = DynamicModule.from_script(
        script,
        overrides=overrides,
        yaml_config=xp.config_file if xp.config_file.exists() else train_config,
    )
    _setup_module(module, env, xp, "evaluate")

    task = module.call_fn("task", caller="evaluate")
    eval_data = module.call_fn("valid_data", caller="evaluate")
    callbacks = module.call_fn("callbacks", caller="evaluate")
    module.serialize(xp.config_file)

    eval_state = tnt.init_eval_state(dataloader=eval_data)
    log.info(f"Evaluating on {eval_data} ...")

    snapshot = torchsnapshot.Snapshot(path=str(snapshot_dir))
    # Also restore state.train_state.progress, so we can log eval results at the proper step
    eval_state._train_state = tnt.PhaseState(dataloader=[])
    state_dict = task.state_dict_for_inference()
    state_dict["train_progress"] = eval_state._train_state.progress
    snapshot.restore(state_dict)

    if isinstance(task.logger, fairseq2.callbacks.WandbLogger):
        # TODO: should MetricLogger abstract over that ?
        wandb_group = module["job.wandb_group"]
        task.logger.prepare()
        try:
            task.logger._wandb_run.use_artifact(f"{wandb_group}:latest")
        except Exception:
            # The artifact may not be "ready" yet (not sure what that mean)
            pass

    tnt.evaluate(eval_state, task, callbacks=callbacks)


def eval_server(
    snapshot_root: Path,
    partition: str = "debug",
    num_gpus: int = 1,
    timeout: datetime.timedelta = datetime.timedelta(minutes=10),
    script: Optional[Path] = None,
    overrides: List[str] = [],
) -> None:
    """Run 'evaluate' on each new snapshot that appear under a given folder

    - snapshot_root: the root folder to monitor
    - partition: partition to use for the evaluate run. Run locally by default.
    - num_gpus: number of gpus for eval
    - script: overrides the "hubconf.py" found in the model snapshot. This can have unexpected results.
    """
    if not snapshot_root.exists():
        raise FileNotFoundError(f"Root folder {snapshot_root} doesn't exist !")
    if script and not script.exists():
        raise FileNotFoundError(f"--script {script} doesn't exist !")

    slurm_args, overrides = _extract_slurm_args(overrides)
    xp_sha = "_" + sha_key(overrides) if overrides else ""

    def _logfile(snapshot: Path) -> Path:
        # Write logs above the snapshot folder, allowing to delete the snapshot
        # without losing the evaluation results
        return snapshot.parent / f"{snapshot.name}.eval{xp_sha}.log"

    def _find_new_snapshots(snapshot_root: Path, treated: Set[Path]) -> Set[Path]:
        warned = False
        while True:
            if not snapshot_root.exists():
                raise FileNotFoundError(
                    f"Folder {snapshot_root} doesn't exists anymore."
                )
            try:
                snapshots = {
                    s
                    for s in snapshot_root.glob("**/epoch_*_step_*")
                    if s.is_dir() and not _logfile(s).exists()
                }
            except FileNotFoundError:
                # This can happen if someone deleted a folder we were traversing
                continue
            snapshots -= treated
            if snapshots:
                return snapshots

            if not warned:
                print(f"No new snapshot found under {snapshot_root}")
                warned = True
            time.sleep(10)

    treated: Set[Path] = set()
    failed: Set[Path] = set()
    timed_out: Set[Path] = set()
    while True:
        queue = list(_find_new_snapshots(snapshot_root, treated))
        # Shuffle the queue otherwise we always prioritize the same run.
        random.shuffle(queue)
        pending = len(queue)
        for snapshot in queue:
            if not snapshot.exists():
                pending -= 1
                continue

            logfile = _logfile(snapshot)
            print(f"Starting evaluation of {snapshot}, logs at {logfile}")
            # Run in a subprocess for better isolation
            eval_cmd = [
                sys.executable,
                "-m",
                "fairseq2.cli",
                "evaluate",
                snapshot,
                f"--partition={partition}",
                f"--num_gpus={num_gpus}",
            ]
            if script:
                eval_cmd += ["--script", script]
            eval_cmd += overrides

            with logfile.open("w", encoding="utf-8") as o:
                try:
                    # TODO allow to run several of those in parallel when using the cluster as the backend
                    eval_process = subprocess.run(eval_cmd, stdout=o, stderr=o, timeout=timeout.total_seconds())  # type: ignore
                    pending -= 1
                    if eval_process.returncode == 0:
                        status = "Evaluated"
                        # TODO: output the metrics in a structured format
                        # tag "best" snapshot
                        treated.add(snapshot)
                    else:
                        status = "Failed"
                        failed.add(snapshot)
                except (subprocess.TimeoutExpired, OSError):
                    pending -= 1
                    status = "Timedout"
                    timed_out.add(snapshot)

            print(
                f"{status} {snapshot} (pending: {pending}, evaluated: {len(treated)}, failed: {len(failed)}, timed_out: {len(timed_out)})"
            )


beam_search_kwargs = {
    "beam_size": 2,
    "max_len": 128,
    "unk_penalty": 1.0,
}


def inference(
    snapshot_dir: Path,
    src_bos: str = "",
    tgt_bos: str = "",
    partition: str = "debug",
    batch_size: int = 16,
    num_gpus: int = 1,
) -> None:
    import fairseq2.generate

    if not snapshot_dir.exists():
        raise FileNotFoundError(f"Snapshot {snapshot_dir} not found.")

    # Currently inference always run locally. This could be an issue for large model
    # TODO: allow distributed inference (this won't work with stdin/stdout)
    assert partition == "debug", "TODO: local inference is supported"

    env = fairseq2.distributed.init(snapshot_dir, partition, num_gpus)
    # Note: it's important to use torch.hub.load here,
    # so we don't make too many assumption on how people store the model.
    task: Seq2Seq = torch.hub.load(
        snapshot_dir, "hub_task", snapshot_dir, source="local", device=env.device
    )

    task.model.eval()

    tty = os.isatty(sys.stdin.fileno())
    if tty:
        batch_size = 1
    strategy = fairseq2.generate.BeamSearchStrategy(
        vocab_info=task.tokenizer,
        **beam_search_kwargs,  # type: ignore
    )

    def gen(batch: List[str]) -> List[str]:
        if not batch:
            return batch
        return strategy.generate_str(
            task.model,
            task.tokenizer,
            batch,
            src_bos=src_bos,
            tgt_bos=tgt_bos,
            device=env.device,
        )

    batch = []
    if tty:
        print("> ", end="", flush=True)
    for line in sys.stdin:
        batch.append(line.strip())
        if len(batch) < batch_size:
            continue
        for translation in gen(batch):
            print(translation)
        if tty:
            print("> ", end="", flush=True)
        batch.clear()

    for translation in gen(batch):
        print(translation)


# TODO: add helper that show all configs expected by a given script


def _extract_slurm_args(overrides: List[str]) -> Tuple[Dict[str, str], List[str]]:
    # TODO: this feels like a hack
    slurm_argslist = [o for o in overrides if o.startswith("slurm.")]
    overrides = [o for o in overrides if not o.startswith("slurm.")]

    slurm_args = {}
    for a in slurm_argslist:
        a = a[len("slurm_") :]
        k, v = a.split("=", 1)
        slurm_args[k] = v
    return slurm_args, overrides


def _setup_module(
    module: DynamicModule, env: fairseq2.distributed.Env, xp: XP, entry_point: str
) -> None:
    module["env"] = env
    module["xp"] = xp
    module["entry_point"] = entry_point
