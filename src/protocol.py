"""
通信协议定义
"""
import json
import socket
import threading
import weakref
from enum import Enum
from typing import Dict, Any


class MessageType(Enum):
    """消息类型枚举"""
    MOUSE_MOVE = "mouse_move"
    MOUSE_CLICK = "mouse_click"
    MOUSE_SCROLL = "mouse_scroll"
    KEY_PRESS = "key_press"
    KEY_RELEASE = "key_release"
    INPUT_SHARE = "input_share"
    CLIPBOARD = "clipboard"
    SCREEN_INFO = "screen_info"
    HANDSHAKE = "handshake"
    HEARTBEAT = "heartbeat"
    TEXT_MESSAGE = "text_message"  # 文本消息
    FILE_OFFER = "file_offer"
    FILE_CHANNEL = "file_channel"
    DESKTOP_REQUEST = "desktop_request"
    DESKTOP_RESPONSE = "desktop_response"
    DESKTOP_STOP = "desktop_stop"
    DESKTOP_CHANNEL = "desktop_channel"
    DESKTOP_AUDIO = "desktop_audio"


class Protocol:
    """协议处理类"""
    
    _send_locks = weakref.WeakKeyDictionary()
    _send_locks_guard = threading.Lock()

    @staticmethod
    def send_message(sock, message: bytes):
        """完整发送一帧；同一连接上的所有发送者共享锁。"""
        with Protocol._send_locks_guard:
            lock = Protocol._send_locks.get(sock)
            if lock is None:
                lock = threading.Lock()
                Protocol._send_locks[sock] = lock
        with lock:
            try:
                if len(message) <= 65536:
                    sock.sendall(message)
                else:
                    # 有超时的连接按块发送，持续有进展的大文件可以传完。
                    view = memoryview(message)
                    for offset in range(0, len(view), 65536):
                        sock.sendall(view[offset:offset + 65536])
            except OSError:
                # 一帧可能只发送了一部分，关闭连接以免下一帧被当成剩余内容。
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                raise

    @staticmethod
    def _receive_exact(sock, size):
        data = bytearray()
        while len(data) < size:
            chunk = sock.recv(min(size - len(data), 65536))
            if not chunk:
                return None
            data.extend(chunk)
        return bytes(data)

    @staticmethod
    def encode(msg_type: MessageType, data: Dict[str, Any]) -> bytes:
        """
        编码消息为字节流
        
        Args:
            msg_type: 消息类型
            data: 消息数据
            
        Returns:
            编码后的字节流
        """
        message = {
            "type": msg_type.value,
            "data": data
        }
        json_str = json.dumps(message)
        # 添加消息长度前缀（4字节）
        msg_bytes = json_str.encode('utf-8')
        length = len(msg_bytes)
        return length.to_bytes(4, byteorder='big') + msg_bytes
    
    @staticmethod
    def decode(data: bytes) -> tuple[MessageType, Dict[str, Any]]:
        """
        解码字节流为消息
        
        Args:
            data: 字节流数据
            
        Returns:
            (消息类型, 消息数据)
        """
        message = json.loads(data.decode('utf-8'))
        msg_type = MessageType(message["type"])
        return msg_type, message["data"]
    
    @staticmethod
    def receive_message(sock, max_size=None) -> tuple[MessageType, Dict[str, Any]] | None:
        """
        从socket接收完整消息
        
        Args:
            sock: socket对象
            
        Returns:
            (消息类型, 消息数据) 或 None (遇到严重错误时)
        """
        try:
            length_bytes = Protocol._receive_exact(sock, 4)
            if length_bytes is None:
                return None
            length = int.from_bytes(length_bytes, byteorder='big')
            if max_size is not None and length > max_size:
                return None
            data = Protocol._receive_exact(sock, length)
            if data is None:
                return None
            return Protocol.decode(data)
        except Exception as e:
            print(f"接收消息错误: {e}")
            return None
