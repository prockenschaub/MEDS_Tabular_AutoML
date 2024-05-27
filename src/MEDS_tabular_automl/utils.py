"""The base class for core dataset processing logic.

Attributes:
    INPUT_DF_T: This defines the type of the allowable input dataframes -- e.g., databases, filepaths,
        dataframes, etc.
    DF_T: This defines the type of internal dataframes -- e.g. polars DataFrames.
"""
import json
from collections.abc import Mapping
from pathlib import Path

import polars as pl
import polars.selectors as cs
import yaml
from omegaconf import DictConfig, OmegaConf

DF_T = pl.LazyFrame
WRITE_USE_PYARROW = True


def parse_flat_feature_column(c: str) -> tuple[str, str, str, str]:
    parts = c.split("/")
    if len(parts) < 3:
        raise ValueError(f"Column {c} is not a valid flat feature column!")
    return (parts[0], "/".join(parts[1:-1]), parts[-1])


def write_df(df: DF_T, fp: Path, **kwargs):
    """Write shard to disk."""
    do_overwrite = kwargs.get("do_overwrite", False)

    if not do_overwrite and fp.is_file():
        raise FileExistsError(f"{fp} exists and do_overwrite is {do_overwrite}!")

    fp.parent.mkdir(exist_ok=True, parents=True)

    if isinstance(df, pl.LazyFrame):
        df.collect().write_parquet(fp, use_pyarrow=WRITE_USE_PYARROW)
    else:
        df.write_parquet(fp, use_pyarrow=WRITE_USE_PYARROW)


def get_flat_col_dtype(col: str) -> pl.DataType:
    """Gets the appropriate minimal dtype for the given flat representation column string."""

    code_type, code, agg = parse_flat_feature_column(col)

    match agg:
        case "sum" | "sum_sqd" | "min" | "max" | "value" | "first":
            return pl.Float32
        case "present":
            return pl.Boolean
        case "count" | "has_values_count":
            return pl.UInt32
        case _:
            raise ValueError(f"Column name {col} malformed!")


def add_missing_cols(flat_df: DF_T, feature_columns: list[str], set_count_0_to_null: bool = False) -> DF_T:
    """Normalizes columns in a DataFrame so all expected columns are present and appropriately typed.

    Parameters:
    - flat_df (DF_T): The DataFrame to be normalized.
    - feature_columns (list[str]): A list of feature column names that should exist in the DataFrame.
    - set_count_0_to_null (bool): A flag indicating whether counts of zero should be converted to nulls.

    Returns:
    - DF_T: The normalized DataFrame with all columns set to the correct type and zero-counts handled
        if specified.

    This function ensures that all necessary columns are added and typed correctly within
    a DataFrame, potentially modifying zero counts to nulls based on the configuration.
    """
    cols_to_add = set(feature_columns) - set(flat_df.columns)
    cols_to_retype = set(feature_columns).intersection(set(flat_df.columns))

    cols_to_add = [(c, get_flat_col_dtype(c)) for c in cols_to_add]
    cols_to_retype = [(c, get_flat_col_dtype(c)) for c in cols_to_retype]

    if "timestamp" in flat_df.columns:
        key_cols = ["patient_id", "timestamp"]
    else:
        key_cols = ["patient_id"]

    flat_df = flat_df.with_columns(
        *[pl.lit(None, dtype=dt).alias(c) for c, dt in cols_to_add],
        *[pl.col(c).cast(dt).alias(c) for c, dt in cols_to_retype],
    ).select(*key_cols, *feature_columns)

    if not set_count_0_to_null:
        return flat_df

    flat_df = flat_df.collect()

    flat_df = flat_df.with_columns(
        pl.when(cs.ends_with("count") != 0).then(cs.ends_with("count")).keep_name()
    ).lazy()
    return flat_df


def get_static_feature_cols(shard_df) -> list[str]:
    """Generates a list of feature column names from the data within each shard based on specified
    configurations.

    Parameters:
    - cfg (dict): Configuration dictionary specifying how features should be evaluated and aggregated.
    - split_to_shard_df (dict): A dictionary of DataFrames, divided by data split (e.g., 'train', 'test').

    Returns:
    - tuple[list[str], dict]: A tuple containing a list of feature columns and a dictionary of code properties
        identified during the evaluation.

    This function evaluates the properties of codes within training data and applies configured
    aggregations to generate a comprehensive list of feature columns for modeling purposes.
    Examples:
    >>> import polars as pl
    >>> data = {'code': ['A', 'A', 'B', 'B', 'C', 'C', 'C'],
    ...         'timestamp': [None, '2021-01-01', '2021-01-01', '2021-01-02', '2021-01-03', '2021-01-04', None], # noqa: E501
    ...         'numerical_value': [1, None, 2, 2, None, None, 3]}
    >>> df = pl.DataFrame(data).lazy()
    >>> get_static_feature_cols(df)
    ['static/A/first', 'static/A/present', 'static/C/first', 'static/C/present']
    """
    feature_columns = []
    static_df = shard_df.filter(pl.col("timestamp").is_null())
    for code in static_df.select(pl.col("code").unique()).collect().to_series():
        static_aggregations = [f"static/{code}/present", f"static/{code}/first"]
        feature_columns.extend(static_aggregations)
    return sorted(feature_columns)


def get_ts_feature_cols(aggregations: list[str], shard_df: DF_T) -> list[str]:
    """Generates a list of feature column names from the data within each shard based on specified
    configurations.

    Parameters:
    - cfg (dict): Configuration dictionary specifying how features should be evaluated and aggregated.
    - split_to_shard_df (dict): A dictionary of DataFrames, divided by data split (e.g., 'train', 'test').

    Returns:
    - tuple[list[str], dict]: A tuple containing a list of feature columns and a dictionary of code properties
        identified during the evaluation.

    This function evaluates the properties of codes within training data and applies configured
    aggregations to generate a comprehensive list of feature columns for modeling purposes.
    Examples:
    >>> import polars as pl
    >>> data = {'code': ['A', 'A', 'B', 'B', 'C', 'C', 'C'],
    ...         'timestamp': [None, '2021-01-01', None, None, '2021-01-03', '2021-01-04', None],
    ...         'numerical_value': [1, None, 2, 2, None, None, 3]}
    >>> df = pl.DataFrame(data).lazy()
    >>> aggs = ['sum', 'count']
    >>> get_ts_feature_cols(aggs, df)
    ['A/count', 'A/sum', 'C/count', 'C/sum']
    """
    feature_columns = []
    ts_df = shard_df.filter(pl.col("timestamp").is_not_null())
    for code in ts_df.select(pl.col("code").unique()).collect().to_series():
        ts_aggregations = [f"{code}/{agg}" for agg in aggregations]
        feature_columns.extend(ts_aggregations)
    return sorted(feature_columns)


def get_flat_rep_feature_cols(cfg: DictConfig, shard_df: DF_T) -> list[str]:
    """Generates a list of feature column names from the data within each shard based on specified
    configurations.

    Parameters:
    - cfg (dict): Configuration dictionary specifying how features should be evaluated and aggregated.
    - shard_df (DF_T): MEDS format dataframe shard.

    Returns:
    - list[str]: list of all feature columns.

    This function evaluates the properties of codes within training data and applies configured
    aggregations to generate a comprehensive list of feature columns for modeling purposes.
    Example:
    >>> data = {'code': ['A', 'A', 'B', 'B'],
    ...         'timestamp': [None, '2021-01-01', None, None],
    ...         'numerical_value': [1, None, 2, 2]}
    >>> df = pl.DataFrame(data).lazy()
    >>> aggs = ['sum', 'count']
    >>> cfg = DictConfig({'aggs': aggs})
    >>> get_flat_rep_feature_cols(cfg, df)
    ['static/A/first', 'static/A/present', 'static/B/first', 'static/B/present', 'A/count', 'A/sum']
    """
    static_feature_columns = get_static_feature_cols(shard_df)
    ts_feature_columns = get_ts_feature_cols(cfg.aggs, shard_df)
    return static_feature_columns + ts_feature_columns


def load_meds_data(MEDS_cohort_dir: str) -> Mapping[str, pl.DataFrame]:
    """Loads the MEDS dataset from disk.

    Args:
        MEDS_cohort_dir: The directory containing the MEDS datasets split by subfolders.
            We expect `train` to be a split so `MEDS_cohort_dir/train` should exist.

    Returns:
        Mapping[str, pl.DataFrame]: Mapping from split name to a polars DataFrame containing the MEDS dataset.

    Example:
    >>> import tempfile
    >>> from pathlib import Path
    >>> MEDS_cohort_dir = Path(tempfile.mkdtemp())
    >>> for split in ["train", "val", "test"]:
    ...     split_dir = MEDS_cohort_dir / split
    ...     split_dir.mkdir()
    ...     pl.DataFrame({"patient_id": [1, 2, 3]}).write_parquet(split_dir / "data.parquet")
    >>> split_to_df = load_meds_data(MEDS_cohort_dir)
    >>> assert "train" in split_to_df
    >>> assert len(split_to_df) == 3
    >>> assert len(split_to_df["train"]) == 1
    >>> assert isinstance(split_to_df["train"][0], pl.DataFrame)
    """
    MEDS_cohort_dir = Path(MEDS_cohort_dir)
    meds_fps = list(MEDS_cohort_dir.glob("*/*.parquet"))
    splits = {fp.parent.stem for fp in meds_fps}
    split_to_fps = {split: [fp for fp in meds_fps if fp.parent.stem == split] for split in splits}
    split_to_df = {
        split: [pl.scan_parquet(fp) for fp in split_fps] for split, split_fps in split_to_fps.items()
    }
    return split_to_df


def setup_environment(cfg: DictConfig):
    # check output dir
    flat_dir = Path(cfg.tabularized_data_dir) / "flat_reps"
    assert flat_dir.exists()

    # load MEDS data
    split_to_df = load_meds_data(cfg.MEDS_cohort_dir)
    feature_columns = json.load(open(flat_dir / "feature_columns.json"))

    # Check that the stored config matches the current config
    with open(flat_dir / "config.yaml") as file:
        yaml_config = yaml.safe_load(file)
        stored_config = OmegaConf.create(yaml_config)
    assert stored_config == cfg, "Stored config does not match current config."
    return flat_dir, split_to_df, feature_columns
