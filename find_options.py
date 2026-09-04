import zipfile

z = zipfile.ZipFile('tools/scrcpy-server')
data = z.read('classes.dex')

needles = [
    'raw_stream', 'raw_video_stream', 'send_stream_meta',
    'send_device_meta', 'send_frame_meta', 'send_dummy_byte',
    'tunnel_forward', 'force_adb_forward', 'audio=false', 'control=false',
    'video=false', 'no_audio', 'no_video', 'cleanup', 'scid'
]
for n in needles:
    found = n.encode('utf-8') in data
    print(n, 'FOUND' if found else 'NOT FOUND')
