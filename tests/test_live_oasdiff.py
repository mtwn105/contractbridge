import shutil

import pytest

from contractbridge.analysis import OasDiff


@pytest.mark.skipif(not shutil.which('oasdiff'), reason='Pinned real oasdiff is unavailable; CI installs it explicitly')
async def test_real_oasdiff_reports_removed_route(core):
    _, old, new, _ = core
    result = await OasDiff().analyze(old, new)
    assert result['engine'] == 'oasdiff'
    assert result['raw']
    assert any(c['kind'] == 'ROUTE_CHANGED' for c in result['changes'])
