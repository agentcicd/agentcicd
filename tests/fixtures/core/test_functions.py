"""
Comprehensive tests for agentcicd_fixtures.core.functions module.

Tests all function types: Function, BatchFunction, RowFunction,
RowExplodeFunction, and AggregateFunction with realistic use cases.
"""
import pytest
from typing import List, Tuple
import pyarrow as pa
import pandas as pd

from agentcicd.fixtures.core.function import (
    Function,
    BatchFunction,
    RowFunction,
    RowExplodeFunction,
    AggregateFunction,
)
from agentcicd.fixtures.core.types import (
    DType,
    FType,
    Json,
    StringType,
    IntType,
    FloatType,
    JsonType,
)


# ============================================================================
# Test Function (Base Class)
# ============================================================================

class TextNormalizerFunction(Function):
    """Normalize text by converting to lowercase and stripping whitespace."""

    def input_schema(self) -> Tuple[DType, ...]:
        return (StringType(),)

    def output_schema(self) -> DType:
        return StringType()

    def ftype(self) -> FType:
        return FType.BATCH_FUNCTION

    def setup(self) -> None:
        self.normalize_count = 0

    def teardown(self) -> None:
        pass

    def execute(self, text: str) -> str:
        self.normalize_count += 1
        return text.lower().strip()


class TestFunction:
    """Tests for the base Function class."""

    def test_function_direct_execute(self):
        """Test calling execute directly."""
        func = TextNormalizerFunction()
        func.setup()
        result = func.execute("  HELLO WORLD  ")
        assert result == "hello world"
        func.teardown()

    def test_function_call_with_lifecycle(self):
        """Test __call__ invokes setup and teardown."""
        func = TextNormalizerFunction()
        result = func("  HELLO WORLD  ")
        assert result == "hello world"
        assert func.normalize_count == 1

    def test_function_schema_methods(self):
        """Test schema declaration methods."""
        func = TextNormalizerFunction()
        assert len(func.input_schema()) == 1
        assert isinstance(func.input_schema()[0], StringType)
        assert isinstance(func.output_schema(), StringType)
        assert func.ftype() == FType.BATCH_FUNCTION


# ============================================================================
# Test BatchFunction
# ============================================================================

class SentimentScorer(BatchFunction):
    """Score sentiment of text batches (simplified example)."""

    def input_schema(self) -> Tuple[DType, ...]:
        return (JsonType(),)

    def output_schema(self) -> DType:
        return JsonType()

    def ftype(self) -> FType:
        return FType.BATCH_FUNCTION

    def setup(self) -> None:
        # In real use case, might load ML model here
        self.positive_words = {"good", "great", "excellent", "amazing"}
        self.negative_words = {"bad", "terrible", "awful", "poor"}

    def teardown(self) -> None:
        pass

    def transform(self, texts: List[Json]) -> List[Json]:
        """Score each text in the batch."""
        results = []
        for text in texts:
            if not isinstance(text, str):
                results.append({"text": text, "score": 0.0})
                continue

            words = text.lower().split()
            positive_count = sum(1 for w in words if w in self.positive_words)
            negative_count = sum(1 for w in words if w in self.negative_words)
            score = (positive_count - negative_count) / max(len(words), 1)

            results.append({"text": text, "score": score})

        return results


class TestBatchFunction:
    """Tests for BatchFunction with realistic sentiment analysis use case."""

    def test_batch_sentiment_scoring(self):
        """Test batch processing of sentiment analysis."""
        scorer = SentimentScorer()
        scorer.setup()

        # Create test batch
        texts = pa.array(["This is great", "This is bad", "This is good"])

        # Process batch
        results = list(scorer.execute(texts))

        assert len(results) == 1  # One output batch
        output_data = results[0].to_pylist()
        assert len(output_data) == 3

        # Verify sentiment scores were calculated
        assert all(isinstance(item, dict) for item in output_data)
        assert all("score" in item for item in output_data)

    def test_batch_empty_input(self):
        """Test batch function with empty input."""
        scorer = SentimentScorer()
        scorer.setup()

        empty_batch = pa.array([])
        results = list(scorer.execute(empty_batch))

        assert len(results) == 1
        assert len(results[0]) == 0


# ============================================================================
# Test RowFunction
# ============================================================================

class PriceCalculator(RowFunction):
    """Calculate final price with tax for each product."""

    def input_schema(self) -> Tuple[DType, ...]:
        return (FloatType(), FloatType())  # price, tax_rate

    def output_schema(self) -> DType:
        return FloatType()

    def ftype(self) -> FType:
        return FType.BATCH_FUNCTION

    def setup(self) -> None:
        self.calculation_count = 0

    def teardown(self) -> None:
        pass

    def transform(self, price: Json, tax_rate: Json) -> Json:
        """Calculate price + tax for a single row."""
        self.calculation_count += 1
        if not isinstance(price, (int, float)) or not isinstance(tax_rate, (int, float)):
            return 0.0
        return round(float(price) * (1 + float(tax_rate)), 2)


class TestRowFunction:
    """Tests for RowFunction with realistic price calculation."""

    def test_row_price_calculation(self):
        """Test row-wise price calculations."""
        calculator = PriceCalculator()
        calculator.setup()

        # Product prices and tax rates
        prices = pa.array([100.0, 200.0, 50.0])
        tax_rates = pa.array([0.1, 0.15, 0.08])

        results = list(calculator.execute(prices, tax_rates))

        assert len(results) == 1
        final_prices = results[0].to_pylist()

        # Verify calculations: price * (1 + tax_rate)
        assert final_prices[0] == 110.0  # 100 * 1.1
        assert final_prices[1] == 230.0  # 200 * 1.15
        assert final_prices[2] == 54.0   # 50 * 1.08

        assert calculator.calculation_count == 3

    def test_row_function_single_column(self):
        """Test row function with single input column."""

        class Doubler(RowFunction):
            def input_schema(self) -> Tuple[DType, ...]:
                return (IntType(),)

            def output_schema(self) -> DType:
                return IntType()

            def ftype(self) -> FType:
                return FType.BATCH_FUNCTION

            def setup(self) -> None:
                pass

            def teardown(self) -> None:
                pass

            def transform(self, value: Json) -> Json:
                return value * 2 if isinstance(value, (int, float)) else 0

        doubler = Doubler()
        numbers = pa.array([1, 2, 3, 4, 5])

        results = list(doubler.execute(numbers))
        doubled = results[0].to_pylist()

        assert doubled == [2, 4, 6, 8, 10]


# ============================================================================
# Test RowExplodeFunction
# ============================================================================

class TagExploder(RowExplodeFunction):
    """Explode product tags into separate rows."""

    def input_schema(self) -> Tuple[DType, ...]:
        return (StringType(), JsonType())  # product_id, tags

    def output_schema(self) -> DType:
        return JsonType()

    def ftype(self) -> FType:
        return FType.ROW_EXPLODE_FUNCTION

    def setup(self) -> None:
        pass

    def teardown(self) -> None:
        pass

    def explode(self, product_id: Json, tags: Json) -> List[Json]:
        """Explode tags into individual records."""
        if not isinstance(tags, list):
            return []

        return [
            {"product_id": product_id, "tag": tag}
            for tag in tags
        ]


class TestRowExplodeFunction:
    """Tests for RowExplodeFunction with product tag explosion."""

    def test_explode_product_tags(self):
        """Test exploding product tags into separate rows."""
        exploder = TagExploder()

        # Products with their tags
        product_ids = pa.array(["P001", "P002", "P003"])
        tags = pa.array([
            ["electronics", "sale", "featured"],
            ["clothing", "new"],
            ["home", "clearance", "discount", "sale"]
        ])

        results = list(exploder.execute(product_ids, tags))

        # Should get 3 batches (one per product)
        assert len(results) == 3

        # First product: 3 tags
        batch1 = results[0].to_pylist()
        assert len(batch1) == 3
        assert batch1[0]["product_id"] == "P001"
        assert batch1[0]["tag"] == "electronics"

        # Second product: 2 tags
        batch2 = results[1].to_pylist()
        assert len(batch2) == 2

        # Third product: 4 tags
        batch3 = results[2].to_pylist()
        assert len(batch3) == 4

    def test_explode_empty_tags(self):
        """Test explosion with empty tag lists."""
        exploder = TagExploder()

        product_ids = pa.array(["P001", "P002"])
        tags = pa.array([[], ["tag1"]])

        results = list(exploder.execute(product_ids, tags))

        assert len(results) == 2
        # First product has no tags
        assert len(results[0]) == 0
        # Second product has one tag
        assert len(results[1]) == 1


# ============================================================================
# Test AggregateFunction
# ============================================================================

class SalesAggregator(AggregateFunction):
    """Aggregate sales data to compute statistics."""

    def input_schema(self) -> Tuple[DType, ...]:
        return (FloatType(),)  # sales amounts

    def output_schema(self) -> DType:
        return JsonType()

    def ftype(self) -> FType:
        return FType.AGGREGATE_FUNCTION

    def setup(self) -> None:
        pass

    def teardown(self) -> None:
        pass

    def aggregate(self, sales_amounts: List[Json]) -> Json:
        """Compute sales statistics."""
        amounts = [s for s in sales_amounts if isinstance(s, (int, float))]

        if not amounts:
            return {
                "total": 0.0,
                "average": 0.0,
                "count": 0,
                "min": 0.0,
                "max": 0.0
            }

        return {
            "total": sum(amounts),
            "average": sum(amounts) / len(amounts),
            "count": len(amounts),
            "min": min(amounts),
            "max": max(amounts)
        }


class TestAggregateFunction:
    """Tests for AggregateFunction with sales aggregation."""

    def test_aggregate_sales_statistics(self):
        """Test aggregating sales amounts into statistics."""
        aggregator = SalesAggregator()

        # Daily sales amounts
        sales = pd.Series([100.0, 150.0, 200.0, 175.0, 225.0])

        result = aggregator.execute(sales)
        assert isinstance(result, dict)

        assert result["total"] == 850.0
        assert result["average"] == 170.0
        assert result["count"] == 5
        assert result["min"] == 100.0
        assert result["max"] == 225.0

    def test_aggregate_empty_series(self):
        """Test aggregation with empty data."""
        aggregator = SalesAggregator()
        empty_sales = pd.Series([], dtype=float)

        result = aggregator.execute(empty_sales)
        assert isinstance(result, dict)

        assert result["total"] == 0.0
        assert result["count"] == 0

    def test_aggregate_single_value(self):
        """Test aggregation with single value."""
        aggregator = SalesAggregator()
        sales = pd.Series([42.5])

        result = aggregator.execute(sales)
        assert isinstance(result, dict)

        assert result["total"] == 42.5
        assert result["average"] == 42.5
        assert result["count"] == 1
        assert result["min"] == 42.5
        assert result["max"] == 42.5


# ============================================================================
# Integration Tests
# ============================================================================

class TestIntegration:
    """Integration tests combining multiple function types."""

    def test_function_lifecycle_with_exception(self):
        """Test that teardown is called even when execute raises exception."""

        class FailingFunction(Function):
            def __init__(self):
                self.setup_called = False
                self.teardown_called = False

            def setup(self) -> None:
                self.setup_called = True

            def teardown(self) -> None:
                self.teardown_called = True

            def execute(self, value: str) -> str:
                raise ValueError("Test error")

            def input_schema(self) -> Tuple[DType, ...]:
                return (StringType(),)

            def output_schema(self) -> DType:
                return StringType()

            def ftype(self) -> FType:
                return FType.BATCH_FUNCTION

        func = FailingFunction()

        with pytest.raises(ValueError):
            func("test")

        assert func.setup_called
        assert func.teardown_called

    def test_multiple_function_chaining(self):
        """Test using multiple functions in sequence (simulation)."""
        # Normalize text
        normalizer = TextNormalizerFunction()
        text = normalizer("  HELLO WORLD  ")
        assert text == "hello world"

        # Calculate price
        calculator = PriceCalculator()
        calculator.setup()
        prices = pa.array([100.0])
        tax_rates = pa.array([0.1])
        results = list(calculator.execute(prices, tax_rates))
        assert results[0].to_pylist()[0] == 110.0
