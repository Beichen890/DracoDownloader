"""
自研 BitTorrent 协议实现
没有任何 GPL 依赖，纯 Python 自研
支持 DHT (Kademlia), Peer Wire Protocol, Bencoding
"""

from .magnet import MagnetParser, MagnetLink
from .dht import DHTClient, Node, RoutingTable
from .peer import Peer, PeerConnection, MSG_CHOKE, MSG_UNCHOKE, MSG_INTERESTED, \
    MSG_NOT_INTERESTED, MSG_HAVE, MSG_BITFIELD, MSG_REQUEST, MSG_PIECE, MSG_CANCEL, MSG_PORT
from .downloader import BTDownloader, TorrentMeta, Piece
from .bencode import encode, decode, decode_torrent, info_hash as bencode_info_hash
from .loaders import ResolvedTorrent, resolve as resolve_source
from .trackers import (
    WebTrackerFetcher, merge_trackers, enrich_trackers, FALLBACK_TRACKERS,
)
from .seeding import SeedingPolicy, SeedingStats, SeedingController

__all__ = [
    "MagnetParser", "MagnetLink",
    "DHTClient", "Node", "RoutingTable",
    "Peer", "PeerConnection",
    "MSG_CHOKE", "MSG_UNCHOKE", "MSG_INTERESTED",
    "MSG_NOT_INTERESTED", "MSG_HAVE", "MSG_BITFIELD",
    "MSG_REQUEST", "MSG_PIECE", "MSG_CANCEL", "MSG_PORT",
    "BTDownloader", "TorrentMeta", "Piece",
    "encode", "decode", "decode_torrent", "bencode_info_hash",
    # 多源加载
    "ResolvedTorrent", "resolve_source",
    # Web Tracker
    "WebTrackerFetcher", "merge_trackers", "enrich_trackers", "FALLBACK_TRACKERS",
    # 做种
    "SeedingPolicy", "SeedingStats", "SeedingController",
]
