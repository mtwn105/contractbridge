from contractbridge.telemetry import VERIFICATION_MESSAGE, scrub_event


def test_exception_payload_and_credentials_are_scrubbed():
    secret = 'private-customer-payload'
    event = {'request': {'headers': {'Authorization': secret}, 'data': secret}, 'user': {'email': secret},
             'breadcrumbs': [secret], 'extra': {'payload': secret}, 'message': secret,
             'exception': {'values': [{'type': 'ValueError', 'value': secret, 'stacktrace': {'frames': [
                 {'filename': 'workflow.py', 'lineno': 52, 'function': 'execute', 'vars': {'request': secret}}]}}]}}
    result = scrub_event(event)
    assert secret not in str(result)
    assert result['exception']['values'][0]['stacktrace']['frames'][0] == {'filename': 'workflow.py', 'lineno': 52, 'function': 'execute'}


def test_verification_message_survives_but_user_context_does_not():
    event = {'user': {'id': 'private'}, 'exception': {'values': [{'type': 'MonitoringVerificationError', 'value': VERIFICATION_MESSAGE}]}}
    assert scrub_event(event) == {'exception': {'values': [{'type': 'MonitoringVerificationError', 'value': VERIFICATION_MESSAGE}]}}
