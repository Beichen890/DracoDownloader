"""
自研 BitTorrent 协议实现
无任何 GPL 依赖，纯 Python 自研
支持 DHT (Kademlia), Peer Wire Protocol, Bencoding
"""

from .magnet import MagnetParser, MagnetLink
from .dht import DHTClient, Node, RoutingTable
from .peer import Peer, PeerConnection, MSG_CHOKE, MSG_UNCHOKE, MSG_INTERESTED, \
    MSG_NOT_INTERESTED, MSG_HAVE, MSG_BITFIELD, MSG_REQUEST, MSG_PIECE, MSG_CANCEL, MSG_PORT
from .downloader import BTDownloader, TorrentMeta, Piece
from .bencode import encode, decode, decode_torrent, info_hash as bencode_info_hash

__all__ = [
    "MagnetParser", "MagnetLink",
    "DHTClient", "Node", "RoutingTable",
    "Peer", "PeerConnection",
    "MSG_CHOKE", "MSG_UNCHOKE", "MSG_INTERESTED",
    "MSG_NOT_INTERESTED", "MSG_HAVE", "MSG_BITFIELD",
    "MSG_REQUEST", "MSG_PIECE", "MSG_CANCEL", "MSG_PORT",
    "BTDownloader", "TorrentMeta", "Piece",
    "encode", "decode", "decode_torrent", "bencode_info_hash",
]
