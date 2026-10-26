"""The Windows WeChat edge (edge-protocol.md): Windows receives and sends WeChat messages for
the Mac business service.

- ``protocol``: names, limits and checks both sides share;
- ``client``: the Mac side's client;
- ``fake``: a fake Windows edge for the Mac side's tests;
- ``http``: the HTTP of ``/edge/*`` (the real edge and the fake answer through it).

These four are standard library only and run on Python 3.9.  Nothing is imported here, so
``from src.edge.client import EdgeClient`` pulls in nothing of the Windows bot.
"""
