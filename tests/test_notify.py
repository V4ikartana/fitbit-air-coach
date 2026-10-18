from fitbit_air.notify import notify


def test_notify_calls_osascript_with_escaped_text():
    calls = []
    notify("T", 'say "hi"', run=lambda args, **kw: calls.append(args))
    assert calls == [["osascript", "-e", 'display notification "say \\"hi\\"" with title "T"']]
