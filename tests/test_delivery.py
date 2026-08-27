from rpt_agent.services.delivery import apply_twilio_message_status


class _Result:
    rowcount = 1


class _Connection:
    def __init__(self):
        self.queries = []

    def execute(self, query, params):
        self.queries.append((query, params))
        return _Result()


def test_twilio_delivery_updates_are_durable_and_forward_only():
    conn = _Connection()
    matched = apply_twilio_message_status(
        conn,
        {"MessageSid": "SM-test", "MessageStatus": "delivered"},
    )
    assert matched == 3
    assert len(conn.queries) == 3
    assert all("status='delivered'" in query for query, _params in conn.queries)
    assert all(params[-1] == "SM-test" for _query, params in conn.queries)
