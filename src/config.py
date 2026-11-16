"""
配置管理模块
"""
import json
import os
from typing import List, Dict
from datetime import datetime
from pathlib import Path
import sys


def default_config_file():
    if '__compiled__' not in globals() and not getattr(sys, 'frozen', False):
        return 'config.json'
    if sys.platform == 'darwin':
        folder = Path.home() / 'Library/Application Support/share-input'
    elif sys.platform == 'win32':
        folder = Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData/Local') / 'share-input'
    else:
        folder = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'share-input'
    return str(folder / 'config.json')


class Config:
    """配置管理类"""
    
    def __init__(self, config_file=None):
        self.config_file = config_file if config_file is not None else default_config_file()
        self.config = self._load_config()
    
    def _load_config(self) -> Dict:
        """加载配置"""
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except:
                return self._default_config()
        return self._default_config()
    
    def _default_config(self) -> Dict:
        """默认配置"""
        return {
            'server_history': [],
            'client_history': [],
            'last_server': '',
            'last_port': 9999
        }
    
    def save(self):
        """保存配置"""
        try:
            Path(self.config_file).parent.mkdir(parents=True, exist_ok=True)
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"保存配置失败: {e}")
    
    def add_server_history(self, ip: str, name: str = ''):
        """添加服务器历史"""
        entry = {
            'ip': ip,
            'name': name or ip,
            'last_connected': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        
        # 移除旧的相同IP记录
        self.config['server_history'] = [
            h for h in self.config['server_history'] if h['ip'] != ip
        ]
        
        # 添加到开头
        self.config['server_history'].insert(0, entry)
        
        # 只保留最近10条
        self.config['server_history'] = self.config['server_history'][:10]
        
        self.config['last_server'] = ip
        self.save()
    
    def get_server_history(self) -> List[Dict]:
        """获取服务器历史"""
        return self.config.get('server_history', [])
    
    def get_last_server(self) -> str:
        """获取最后连接的服务器"""
        return self.config.get('last_server', '')
    
    def add_client_connection(self, ip: str, system: str = 'Unknown'):
        """添加客户端连接记录"""
        entry = {
            'ip': ip,
            'system': system,
            'last_connected': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        
        # 移除旧的相同IP记录
        self.config['client_history'] = [
            h for h in self.config.get('client_history', []) if h['ip'] != ip
        ]
        
        # 添加到开头
        self.config['client_history'].insert(0, entry)
        
        # 只保留最近20条
        self.config['client_history'] = self.config['client_history'][:20]
        
        self.save()
    
    def get_client_history(self) -> List[Dict]:
        """获取客户端历史"""
        return self.config.get('client_history', [])
