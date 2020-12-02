def test_data_pipeline():
    from speechbrain.utils.data_pipeline import DataPipeline

    pipeline = DataPipeline.from_configuration(
        funcs={
            "foo": {"func": lambda x: x.lower(), "argnames": ["text"]},
            "bar": {"func": lambda x: x[::-1], "argnames": ["foo"]},
        },
        output_names=["bar"],
    )
    result = pipeline({"text": "Test"})
    assert result["bar"] == "tset"
    pipeline = DataPipeline()
    pipeline.add_func(
        "foobar", func=lambda x, y: x + y, argnames=["foo", "bar"]
    )
    pipeline.output_names.append("foobar")
    result = pipeline({"foo": 1, "bar": 2})
    assert result["foobar"] == 3
    pipeline = DataPipeline()
    from unittest.mock import MagicMock, Mock

    watcher = Mock()
    pipeline.add_func("foobar", func=watcher, argnames=["foo", "bar"])
    result = pipeline({"foo": 1, "bar": 2})
    assert not watcher.called
    pipeline = DataPipeline()
    watcher = MagicMock(return_value=3)
    pipeline.add_func("foobar", func=watcher, argnames=["foo", "bar"])
    pipeline.add_func("truebar", func=lambda x: x, argnames=["foobar"])
    pipeline.output_names.append("truebar")
    result = pipeline({"foo": 1, "bar": 2})
    assert watcher.called
    assert result["truebar"] == 3
    pipeline = DataPipeline()
    watcher = MagicMock(return_value=3)
    pipeline.add_func("foobar", func=watcher, argnames=["foo", "bar"])
    pipeline.add_func("truebar", func=lambda x: x, argnames=["foo"])
    pipeline.output_names.append("truebar")
    result = pipeline({"foo": 1, "bar": 2})
    assert not watcher.called
    assert result["truebar"] == 1

    pipeline = DataPipeline()
    watcher = MagicMock(return_value=3)
    pipeline.add_func("foobar", func=watcher, argnames=["foo", "bar"])
    pipeline.output_names.append("foobar")
    pipeline.output_names.append("foo")
    result = pipeline({"foo": 1, "bar": 2})
    assert watcher.called
    assert "foo" in result
    assert "foobar" in result
    assert "bar" not in result
    # Can change the outputs (continues previous tests)
    watcher.reset_mock()
    pipeline.set_output_names("bar")
    result = pipeline({"foo": 1, "bar": 2})
    assert not watcher.called
    assert "foo" not in result
    assert "foobar" not in result
    assert "bar" in result
