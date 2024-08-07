"""The `FilterOperation` class is a subclass of `BaseOperation` that implements a filtering operation on input data using a language model."""

from typing import Dict, List, Any, Tuple, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from jinja2 import Template
from motion.operations.base import BaseOperation
from motion.operations.utils import call_llm, parse_llm_response
from motion.operations.utils import validate_output, rich_as_completed
from litellm import completion_cost
from rich.console import Console


class FilterOperation(BaseOperation):
    def syntax_check(self) -> None:
        """
        Checks the configuration of the FilterOperation for required keys and valid structure.

        Raises:
            ValueError: If required keys are missing or if the output schema structure is invalid.
            TypeError: If the schema in the output configuration is not a dictionary or if the schema value is not of type bool.

        This method checks for the following:
        - Presence of required keys: 'prompt' and 'output'
        - Presence of 'schema' in the 'output' configuration
        - The 'schema' is a non-empty dictionary with exactly one key-value pair
        - The value in the schema is of type bool
        """
        required_keys = ["prompt", "output"]
        for key in required_keys:
            if key not in self.config:
                raise ValueError(
                    f"Missing required key '{key}' in FilterOperation configuration"
                )

        if "schema" not in self.config["output"]:
            raise ValueError("Missing 'schema' in 'output' configuration")

        if not isinstance(self.config["output"]["schema"], dict):
            raise TypeError("'schema' in 'output' configuration must be a dictionary")

        if not self.config["output"]["schema"]:
            raise ValueError("'schema' in 'output' configuration cannot be empty")

        schema = self.config["output"]["schema"]
        if len(schema) != 1:
            raise ValueError(
                "The 'schema' in 'output' configuration must have exactly one key-value pair that maps to a boolean value"
            )

        key, value = next(iter(schema.items()))
        if value not in ["bool", "boolean"]:
            raise TypeError(
                f"The value in the 'schema' must be of type bool, got {value}"
            )

    def execute(self, input_data: List[Dict]) -> Tuple[List[Dict], float]:
        """
        Executes the filter operation on the input data.

        Args:
            input_data (List[Dict]): A list of dictionaries to process.

        Returns:
            Tuple[List[Dict], float]: A tuple containing the filtered list of dictionaries
            and the total cost of the operation.

        This method performs the following steps:
        1. Processes each input item using an LLM model
        2. Validates the output
        3. Filters the results based on the specified filter key
        4. Calculates the total cost of the operation

        The method uses multi-threading to process items in parallel, improving performance
        for large datasets.

        Usage:
        ```python
        from motion.operations import FilterOperation

        config = {
            "prompt": "Determine if the following item is important: {{input}}",
            "output": {
                "schema": {"is_important": "bool"}
            },
            "model": "gpt-3.5-turbo"
        }
        filter_op = FilterOperation(config)
        input_data = [
            {"id": 1, "text": "Critical update"},
            {"id": 2, "text": "Regular maintenance"}
        ]
        results, cost = filter_op.execute(input_data)
        print(f"Filtered results: {results}")
        print(f"Total cost: {cost}")
        ```
        """
        filter_key = next(iter(self.config["output"]["schema"].keys()))

        def _process_filter_item(item: Dict) -> Tuple[Optional[Dict], float]:
            prompt_template = Template(self.config["prompt"])
            prompt = prompt_template.render(input=item)
            response = call_llm(
                self.config.get("model", self.default_model),
                "filter",
                prompt,
                self.config["output"]["schema"],
            )
            item_cost = completion_cost(response)
            output = parse_llm_response(response)[0]
            for key, value in item.items():
                if key not in self.config["output"]["schema"]:
                    output[key] = value
            if validate_output(self.config, output, self.console):
                return output, item_cost
            return None, item_cost

        with ThreadPoolExecutor(max_workers=self.max_threads) as executor:
            futures = [
                executor.submit(_process_filter_item, item) for item in input_data
            ]
            results = []
            total_cost = 0
            for future in rich_as_completed(
                futures,
                total=len(futures),
                desc="Processing filter items",
                leave=True,
                console=self.console,
            ):
                result, item_cost = future.result()
                total_cost += item_cost
                if result is not None and result.get(filter_key, False):
                    results.append(result)

        return results, total_cost
