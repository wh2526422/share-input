"""Capture a Linux playback monitor through PulseAudio or native PipeWire."""
import json
import os
import select
import shutil
import subprocess

from desktop_audio import CHANNELS, PACKET_BYTES, SAMPLE_RATE


def can_capture():
    return bool((shutil.which('pactl') and shutil.which('parec'))
                or (shutil.which('wpctl') and shutil.which('pw-record')))


def _query(*cmd):
    try:
        return subprocess.check_output(cmd, stderr=subprocess.DEVNULL, text=True,
                                       env=dict(os.environ, LC_ALL='C'), timeout=2).strip()
    except (OSError, subprocess.SubprocessError):
        return ''


def _capture_command():
    rate = f'--rate={SAMPLE_RATE}'
    channels = f'--channels={CHANNELS}'
    if shutil.which('pactl') and shutil.which('parec'):
        sink = _query('pactl', 'get-default-sink')
        if not sink:
            sink = next((line.partition(':')[2].strip() for line in _query('pactl', 'info').splitlines()
                         if line.startswith('Default Sink:')), '')
        if sink:
            return ['parec', f'--device={sink}.monitor', '--raw', '--format=s16le',
                    rate, channels, '--latency-msec=20', '--process-time-msec=20']
    if shutil.which('wpctl') and shutil.which('pw-record'):
        info = _query('wpctl', 'inspect', '@DEFAULT_AUDIO_SINK@')
        for line in info.splitlines():
            key, _, value = line.strip(' *').partition(' = ')
            if key == 'node.name' and value:
                name = json.loads(value)
                return ['pw-record', f'--target={name}', '--properties={"stream.capture.sink":true}',
                        '--raw', '--format=s16', rate, channels, '--latency=20ms', '-']
    raise OSError('未找到系统默认输出的监听源，请检查 PulseAudio 或 PipeWire 服务')


class LinuxAudio:
    def __enter__(self):
        command = _capture_command()
        self.tool = command[0]
        self.process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, bufsize=0)
        return self

    def read(self):
        output = self.process.stdout
        if not select.select([output], [], [], 0.04)[0]:
            return b''
        pcm = os.read(output.fileno(), PACKET_BYTES)
        if not pcm:
            raise OSError(f'{self.tool} 已停止输出系统声音')
        return pcm

    def __exit__(self, *args):
        try:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
        finally:
            self.process.stdout.close()
