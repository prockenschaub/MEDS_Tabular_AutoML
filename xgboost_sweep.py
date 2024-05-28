import hydra
from omegaconf import DictConfig, OmegaConf
from pathlib import Path
import xgboost as xgb
import polars as pl
import numpy as np
import pyarrow as pa
import polars.selectors as cs
from sklearn.metrics import mean_absolute_error

import os
from typing import List, Callable


class Iterator(xgb.DataIter):
    def __init__(self, cfg: DictConfig, split: str = "train"):
        """
        Initialize the Iterator with the provided configuration and split.

        Args:
        - cfg (DictConfig): Configuration dictionary.
        - split (str): The data split to use ("train", "tuning", or "held_out").

        """
        self.cfg = cfg
        self.data_path = Path(cfg.tabularized_data_dir)
        self.dynamic_data_path = self.data_path / "summarize" / split
        self.static_data_path = self.data_path / "static" / split

        self._data_shards = [
            x.stem
            for x in self.static_data_path.iterdir()
            if x.is_file() and x.suffix == ".parquet"
        ]
        if cfg.iterator.keep_static_data_in_memory:
            self._static_shards = (
                self._get_static_shards()
            )  # do we want to cache this differently to share across workers or iterators?

        self.codes_set, self.aggs_set, self.min_frequency_set = self._get_inclusion_sets()

        self._it = 0

        # XGBoost will generate some cache files under current directory with the prefix
        # "cache"
        super().__init__(cache_prefix=os.path.join(".", "cache"))

    def _get_inclusion_sets(self) -> tuple[set, set, set]:
        """
        Get the inclusion sets for codes and aggregations.

        Returns:
        - tuple[set, set, set]: Tuple of sets for codes, aggregations, and minimum code frequency.
        """
        codes_set = None
        aggs_set = None
        min_frequency_set = None
        if self.cfg.codes is not None:
            codes_set = set(self.cfg.codes)
        if self.cfg.aggs is not None:
            aggs_set = set(self.cfg.aggs)
        if self.cfg.min_code_inclusion_frequency is not None:
            # given parquet file with code frequencies for overall dataset, find which codes have high enough frequency to be included and make a set of them
            dataset_freuqency = pl.scan_parquet(
                self.data_path / "code_frequencies.parquet" # TODO: make sure this is the right path
            )
            min_frequency_set = set(
                dataset_freuqency.filter(
                    cs.col("frequency") >= self.cfg.min_code_inclusion_frequency
                )
                .select("code")
                .collect()
                .to_numpy()
                .flatten()
            )

        return codes_set, aggs_set, min_frequency_set

    def _get_static_shards(self) -> dict:
        """
        Load static shards into memory.

        Returns:
        - dict: Dictionary with shard names as keys and data frames as values.

        """
        static_shards = {}
        for iter in self._data_shards:
            static_shards[iter] = pl.scan_parquet(
                self.static_data_path / f"{iter}.parquet"
            )
        return static_shards

    def _load_shard(self, idx: int) -> tuple[np.ndarray, np.ndarray]:
        """
        Load a specific shard of data from disk and concatenate with static data.

        Args:
        - idx (int): Index of the shard to load.

        Returns:
        - X (np.ndarray): Feature data frame.
        - y (np.ndarray): Labels.

        """
        # concatinate with static data
        if self.cfg.iterator.keep_static_data_in_memory:
            df = self._static_shards[self._data_shards[idx]]
        else:
            df = pl.scan_parquet(
                self.static_data_path / f"{self._data_shards[idx]}.parquet"
            )

        for window in self.cfg.window_sizes:
            dynamic_df = pl.scan_parquet(
                self.dynamic_data_path / window / f"{self._data_shards[idx]}.parquet"
            )

            columns = dynamic_df.schema.names
            selected_columns = [
                col
                for col in columns
                if (parts := col.split("/"))
                and len(parts) > 3
                and (self.codes_set is None or "/".join(parts[1:-2]) in self.codes_set)
                and (self.min_frequency_set is None or "/".join(parts[1:-2]) in self.min_frequency_set)
                and (self.aggs_set is None or "/".join(parts[-2:]) in self.aggs_set)
            ]
            selected_columns.extend(["patient_id", "timestamp"])
            dynamic_df = dynamic_df.select(selected_columns)
            # Task data
            task_df = pl.scan_parquet(self.data_path / "tasks.parquet") # need to know if this should be done every time or if it is pulled once for all the data... also need to know if this is the right path
            task_df = task_df.rename({col: f"{col}/task" for col in task_df.schema.names}) # TODO: filtering of the tasks??
            df = task_df.join_asof(
                            df,
                            by="subject_id",
                            on="timestamp",
                            strategy="forward" if "-" in window else "backward",
                        )

            df = pl.concat([df, dynamic_df], how="align")

        ### TODO: add in some type checking etc for safety

        ### TODO: Figure out features vs labels --> look at esgpt_baseline for loading in labels based on tasks


        y = df.select(
            [
                col
                for col in df.schema.names
                if col.endswith("/task")
            ]
        )
        X = df.select(
            [
                col
                for col in df.schema.names
                if col not in ["label", "patient_id", "timestamp"]
                and not col.endswith("/task")
            ]
        )

        ### TODO: Figure out best way to export this to dmatrix --> can we use scipy sparse matrix/array? --> likely we will not be able to collect in memory
        return (
            X.collect().to_numpy(),
            y.collect().to_numpy(),
        )  # convert to sparse matrix instead

    def next(self, input_data: Callable):
        """
        Advance the iterator by 1 step and pass the data to XGBoost.  This function is
        called by XGBoost during the construction of ``DMatrix``

        Args:
        - input_data (Callable): A function passed by XGBoost with the same signature as `DMatrix`.

        Returns:
        - int: 0 if end of iteration, 1 otherwise.
        """
        if self._it == len(self._data_shards):
            # return 0 to let XGBoost know this is the end of iteration
            return 0

        # input_data is a function passed in by XGBoost who has the exact same signature of
        # ``DMatrix``
        X, y = self._load_shard(self._it)  # self._data_shards[self._it])
        input_data(data=X, label=y)
        self._it += 1
        # Return 1 to let XGBoost know we haven't seen all the files yet.
        return 1

    def reset(self):
        """
        Reset the iterator to its beginning.

        """
        self._it = 0

    def collect_in_memory(self) -> tuple[np.ndarray, np.ndarray]:
        """
        Collect the data in memory.

        Returns:
        - tuple[np.ndarray, np.ndarray]: Tuple of feature data and labels.

        """
        X = []
        y = []
        for i in range(len(self._data_shards)):
            X_, y_ = self._load_shard(i)
            X.append(X_)
            y.append(y_)
        X = np.concatenate(X, axis=0)
        y = np.concatenate(y, axis=0)
        return X, y


class XGBoostModel:
    def __init__(self, cfg: DictConfig):
        """
        Initialize the XGBoostClassifier with the provided configuration.

        Args:
        - cfg (DictConfig): Configuration dictionary.
        """

        self.cfg = cfg
        self.keep_data_in_memory = getattr(
            getattr(cfg, "model", {}), "keep_data_in_memory", True
        )

        self.itrain = None
        self.ival = None
        self.itest = None

        self.dtrain = None
        self.dval = None
        self.dtest = None

        self.model = None

    def train(self):
        """
        Train the model.

        """
        self._build()
        self.model = xgb.train(
            OmegaConf.to_container(self.cfg.model), self.dtrain
        )  # do we want eval and things?

    def _build(self):
        """
        Build necessary data structures for training.

        """
        if self.keep_data_in_memory:
            self._build_iterators()
            self._build_dmatrix_in_memory()
        else:
            self._build_iterators()
            self._build_dmatrix_from_iterators()

    def _build_dmatrix_in_memory(self):
        """
        Build the DMatrix from the data in memory.

        """
        X_train, y_train = self.itrain.collect_in_memory()
        X_val, y_val = self.ival.collect_in_memory()
        X_test, y_test = self.itest.collect_in_memory()
        self.dtrain = xgb.DMatrix(X_train, label=y_train)
        self.dval = xgb.DMatrix(X_val, label=y_val)
        self.dtest = xgb.DMatrix(X_test, label=y_test)

    def _build_dmatrix_from_iterators(self):
        """
        Build the DMatrix from the iterators.

        """
        self.dtrain = xgb.DMatrix(self.ival)
        self.dval = xgb.DMatrix(self.itest)
        self.dtest = xgb.DMatrix(self.itest)

    def _build_iterators(self):
        """
        Build the iterators for training, validation, and testing.

        """
        self.itrain = Iterator(self.cfg, split="train")
        self.ival = Iterator(self.cfg, split="tuning")
        self.itest = Iterator(self.cfg, split="held_out")

    def evaluate(self) -> float:
        """
        Evaluate the model on the test set.

        Returns:
        - float: Evaluation metric (mae).

        """
        ### TODO: Figure out exactly what we want to do here

        y_pred = self.model.predict(self.dtest)
        y_true = self.dtest.get_label()
        return mean_absolute_error(y_true, y_pred)


@hydra.main(version_base=None, config_path="configs", config_name="tabularize_sweep")
def optimize(cfg: DictConfig) -> float:
    """
    Optimize the model based on the provided configuration.

    Args:
    - cfg (DictConfig): Configuration dictionary.

    Returns:
    - float: Evaluation result.

    """

    model = XGBoostModel(cfg)
    model.train()
    return model.evaluate()


if __name__ == "__main__":
    optimize()
