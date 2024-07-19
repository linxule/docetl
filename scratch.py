import sys
import json
from abc import ABC, abstractmethod
import multiprocessing
from typing import (
    List,
    Any,
    Dict,
    Tuple,
    Optional,
    Set,
    Callable,
    TypeVar,
    Generic,
    Union,
    Iterable,
)
from collections import defaultdict
from enum import Enum
import random
import uuid

K = TypeVar("K")
V = TypeVar("V")
R = TypeVar("R")


class ValidatorAction(Enum):
    PROMPT = "prompt"
    WARN = "warn"
    FAIL = "fail"


class Operator(ABC, Generic[K, V]):
    on_fail: ValidatorAction = ValidatorAction.WARN

    @abstractmethod
    def validate(self, key: K, value: V) -> bool:
        pass

    @abstractmethod
    def correct(self, key: K, value: V, new_value: Any) -> Tuple[K, V]:
        pass

    def get_description(self) -> Optional[str]:
        return None


class Mapper(Operator[K, V]):
    @abstractmethod
    def map(self, key: Any, value: Any) -> List[Tuple[K, V]]:
        pass

    def correct(self, key: K, value: V, new_value: Any) -> Tuple[K, V]:
        try:
            new_key = type(key)(new_value)
            new_value = type(value)(new_value)
            return new_key, new_value
        except (ValueError, TypeError):
            return key, value


class Reducer(Operator[K, V]):
    @abstractmethod
    def reduce(self, key: K, values: List[V]) -> V:
        pass

    def correct(self, key: K, value: V, new_value: Any) -> Tuple[K, V]:
        try:
            new_key = type(key)(new_value)
            new_value = type(value)(new_value)
            return new_key, new_value
        except (ValueError, TypeError):
            return key, value


class Equality(ABC, Generic[K]):
    @abstractmethod
    def precheck(self, x: K, y: K) -> bool:
        pass

    @abstractmethod
    def are_equal(self, x: K, y: K) -> bool:
        pass

    @abstractmethod
    def get_label(self, keys: Set[K]) -> Any:
        pass

    def correct(self, key: K, new_value: Any) -> K:
        try:
            return type(key)(new_value)
        except (ValueError, TypeError):
            return key

    def get_description(self) -> Optional[str]:
        return None


class Operation:
    def __init__(
        self, operator: Union[Mapper, Reducer], equality: Optional[Equality] = None
    ):
        self.operator = operator
        self.equality = equality


class Dataset:
    def __init__(
        self, data: Iterable[Any], num_workers: int = None, enable_tracing: bool = True
    ):
        self.data = [(str(uuid.uuid4()), "", item, {}) for item in data]
        self.num_workers = num_workers or multiprocessing.cpu_count()
        self.operations: List[Operation] = []
        self.enable_tracing = enable_tracing

    def map(self, mapper: Mapper) -> "Dataset":
        self.operations.append(Operation(mapper))
        return self

    def reduce(self, reducer: Reducer, equality: Equality) -> "Dataset":
        self.operations.append(Operation(reducer, equality))
        return self

    def execute(self) -> List[Tuple[str, Any, Any, Dict[str, str]]]:
        current_data = self.data
        for i, operation in enumerate(self.operations):
            current_data = self._apply_operation(current_data, operation, i)
        return current_data

    def _chunk_data(
        self, data: List[Tuple[str, Any, Any, Dict[str, str]]], chunk_size: int
    ) -> List[List[Tuple[str, Any, Any, Dict[str, str]]]]:
        return [data[i : i + chunk_size] for i in range(0, len(data), chunk_size)]

    def _apply_operation(
        self,
        data: List[Tuple[str, Any, Any, Dict[str, str]]],
        operation: Operation,
        op_index: int,
    ) -> List[Tuple[str, Any, Any, Dict[str, str]]]:
        chunk_size = max(len(data) // self.num_workers, 1)
        chunks = self._chunk_data(data, chunk_size)

        with multiprocessing.Pool(self.num_workers) as pool:
            if isinstance(operation.operator, Mapper):
                results = pool.starmap(
                    self._map_worker, [(chunk, operation, op_index) for chunk in chunks]
                )
            else:  # Reducer
                grouped_data = self._group_by_key(data, operation.equality)
                results = pool.starmap(
                    self._reduce_worker,
                    [
                        (key, values, operation, op_index)
                        for key, values in grouped_data.items()
                    ],
                )

        processed_data = []
        errors = []
        for result, error in results:
            processed_data.extend(result)
            errors.extend(error)

        corrected_data = self._handle_validation_errors(
            errors, operation.operator.on_fail, operation.operator
        )

        # Update processed_data with corrected_data
        processed_data_dict = {
            record_id: (key, value, trace)
            for record_id, key, value, trace in processed_data
        }
        for record_id, key, value, trace in corrected_data:
            processed_data_dict[record_id] = (key, value, trace)

        # Apply labels if it's a reduce operation
        if isinstance(operation.operator, Reducer) and operation.equality:
            labeled_data = self._apply_labels(
                processed_data_dict, operation.equality, op_index
            )
        else:
            labeled_data = [
                (record_id, key, value, trace)
                for record_id, (key, value, trace) in processed_data_dict.items()
            ]

        return labeled_data

    def _map_worker(
        self,
        chunk: List[Tuple[str, Any, Any, Optional[Dict[str, str]]]],
        operation: Operation,
        op_index: int,
    ) -> Tuple[
        List[Tuple[str, Any, Any, Optional[Dict[str, str]]]],
        List[Tuple[str, Any, Any, Optional[Dict[str, str]]]],
    ]:
        result = []
        errors = []
        for record_id, key, value, trace in chunk:
            mapped_items = operation.operator.map(key, value)
            for mapped_key, mapped_value in mapped_items:
                new_id = str(uuid.uuid4())
                new_trace = (
                    trace.copy() if self.enable_tracing and trace is not None else None
                )
                if (
                    self.enable_tracing
                    and new_trace is not None
                    and operation.operator.get_description()
                ):
                    new_trace[f"op_{op_index}"] = operation.operator.get_description()
                result.append((new_id, mapped_key, mapped_value, new_trace))
                if not operation.operator.validate(mapped_key, mapped_value):
                    errors.append((new_id, mapped_key, mapped_value, new_trace))
        return result, errors

    def _reduce_worker(
        self,
        key: Any,
        values: List[Tuple[str, Any, Optional[Dict[str, str]]]],
        operation: Operation,
        op_index: int,
    ) -> Tuple[
        List[Tuple[str, Any, Any, Optional[Dict[str, str]]]],
        List[Tuple[str, Any, Any, Optional[Dict[str, str]]]],
    ]:
        record_ids, values, traces = zip(*values)
        reduced_value = operation.operator.reduce(key, list(values))
        new_id = str(uuid.uuid4())
        combined_trace = None
        if self.enable_tracing:
            combined_trace = {}
            for trace in traces:
                if trace is not None:
                    combined_trace.update(trace)
            if operation.operator.get_description():
                combined_trace[f"op_{op_index}"] = operation.operator.get_description()
        if operation.operator.validate(key, reduced_value):
            return [(new_id, key, reduced_value, combined_trace)], []
        else:
            return [(new_id, key, reduced_value, combined_trace)], [
                (new_id, key, reduced_value, combined_trace)
            ]

    def _handle_validation_errors(
        self,
        errors: List[Tuple[str, Any, Any, Dict[str, str]]],
        action: ValidatorAction,
        operator: Operator,
    ) -> List[Tuple[str, Any, Any, Dict[str, str]]]:
        if not errors:
            return []

        if action == ValidatorAction.PROMPT:
            print("Validation Errors:", file=sys.stderr)
            for record_id, error_key, error_value, error_trace in errors:
                print(
                    f"  - ID: {record_id}, Key: {error_key}, Value: {error_value}",
                    file=sys.stderr,
                )
            print(
                "\nEnter corrections as a JSON dictionary mapping ID to new value, or press Enter to skip:",
                file=sys.stderr,
            )
            try:
                user_input = sys.stdin.readline().strip()
                if user_input:
                    corrections = json.loads(user_input)
                    corrected_errors = []
                    for record_id, error_key, error_value, error_trace in errors:
                        if record_id in corrections:
                            new_value = corrections[record_id]
                            corrected_key, corrected_value = operator.correct(
                                error_key, error_value, new_value
                            )
                            corrected_errors.append(
                                (record_id, corrected_key, corrected_value, error_trace)
                            )
                    return corrected_errors
                return []
            except json.JSONDecodeError:
                print("Invalid JSON input. Skipping corrections.", file=sys.stderr)
            except KeyboardInterrupt:
                print("\nOperation aborted by user.", file=sys.stderr)
                sys.exit(1)
        elif action == ValidatorAction.WARN:
            for record_id, error_key, error_value, error_trace in errors:
                print(
                    f"Warning: Validation failed for ID: {record_id}, Key: {error_key}, Value: {error_value}",
                    file=sys.stderr,
                )
        elif action == ValidatorAction.FAIL:
            error_message = "\n".join(
                f"ID: {record_id}, Key: {error_key}, Value: {error_value}"
                for record_id, error_key, error_value, _ in errors
            )
            raise ValueError(f"Validation Errors:\n{error_message}")

        return []

    def _apply_labels(
        self,
        data: Dict[str, Tuple[Any, Any, Optional[Dict[str, str]]]],
        equality: Equality,
        op_index: int,
    ) -> List[Tuple[str, Any, Any, Optional[Dict[str, str]]]]:
        labeled_data = []
        unique_keys = set(key for key, _, _ in data.values())
        while unique_keys:
            key = unique_keys.pop()
            equal_keys = {key}
            for other_key in list(unique_keys):
                if equality.are_equal(key, other_key):
                    equal_keys.add(other_key)
                    unique_keys.remove(other_key)
            label = equality.get_label(equal_keys)
            for record_id, (old_key, value, trace) in data.items():
                if old_key in equal_keys:
                    new_trace = (
                        trace.copy()
                        if self.enable_tracing and trace is not None
                        else None
                    )
                    if (
                        self.enable_tracing
                        and new_trace is not None
                        and equality.get_description()
                    ):
                        new_trace[f"op_{op_index}_equality"] = (
                            equality.get_description()
                        )
                    labeled_data.append((record_id, label, value, new_trace))
        return labeled_data

    def _group_by_key(
        self, data: List[Tuple[str, Any, Any, Dict[str, str]]], equality: Equality
    ) -> Dict[Any, List[Tuple[str, Any, Dict[str, str]]]]:
        grouped_data = defaultdict(list)
        unique_keys = []

        for record_id, key, value, trace in data:
            for unique_key in unique_keys:
                if equality.precheck(key, unique_key) and equality.are_equal(
                    key, unique_key
                ):
                    grouped_data[unique_key].append((record_id, value, trace))
                    break
            else:
                unique_keys.append(key)
                grouped_data[key].append((record_id, value, trace))

        return dict(grouped_data)


# Example implementations
class IdentityMapper(Mapper[float, int]):
    on_fail = ValidatorAction.PROMPT

    def map(self, key: Any, value: int) -> List[Tuple[float, int]]:
        return [(float(value), value)]

    def validate(self, key: float, value: int) -> bool:
        if random.random() < 0.2:
            return False
        return 0 <= key <= 10 and 1 <= value <= 100

    def get_description(self) -> str:
        return "Identity Mapper: Convert value to float key"


class SquareMapper(Mapper[float, int]):
    on_fail = ValidatorAction.WARN

    def map(self, key: float, value: int) -> List[Tuple[float, int]]:
        return [(key, value**2)]

    def validate(self, key: float, value: int) -> bool:
        return value >= 0

    def get_description(self) -> str:
        return "Square Mapper: Square the value"


class SumReducer(Reducer[float, int]):
    on_fail = ValidatorAction.WARN

    def reduce(self, key: float, values: List[int]) -> int:
        return sum(values)

    def validate(self, key: float, reduced_value: int) -> bool:
        return 1 <= reduced_value <= 1000000  # Increased upper bound due to squaring

    def get_description(self) -> str:
        return "Sum Reducer: Sum all values"


class FuzzyEquality(Equality[float]):
    def __init__(self, tolerance: float):
        self.tolerance = tolerance

    def precheck(self, x: float, y: float) -> bool:
        return abs(x - y) <= 2 * self.tolerance

    def are_equal(self, x: float, y: float) -> bool:
        return abs(x - y) <= self.tolerance

    def get_label(self, keys: Set[float]) -> float:
        min_key = min(keys)
        max_key = max(keys)
        return sum(keys) / len(keys)

    def get_description(self) -> str:
        return f"Fuzzy Equality: Group keys within {self.tolerance} tolerance"


if __name__ == "__main__":
    input_data = range(1, 10)  # Numbers from 1 to 9

    dataset = Dataset(input_data, enable_tracing=True)
    result = (
        dataset.map(IdentityMapper())
        .reduce(SumReducer(), FuzzyEquality(tolerance=2))
        .map(SquareMapper())
        .reduce(SumReducer(), FuzzyEquality(tolerance=5))
        .execute()
    )

    # Print results in a more readable format
    for record_id, key, value, trace in sorted(result, key=lambda x: x[1]):
        print(f"Record ID: {record_id}")
        print(f"Group {key}: Final Result = {value}")
        print("  Trace:")
        for op, description in trace.items():
            print(f"    {op}: {description}")
        print()
