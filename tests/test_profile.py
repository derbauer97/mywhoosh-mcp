from mywhoosh_mcp.client import _flat, _ftp


def test_flat_and_ftp():
    pd = {"PlayerDataStruct": {"PlayerPersonalStruct": {"FtpPlayer": 227.0, "Coins": 3}}, "PlayerGameData": {"x": 1}}
    assert _ftp(pd) == 227.0
    assert _ftp({}) is None
    assert _flat(pd) == {"PlayerDataStruct.PlayerPersonalStruct.FtpPlayer": 227.0,
                         "PlayerDataStruct.PlayerPersonalStruct.Coins": 3, "PlayerGameData.x": 1}
