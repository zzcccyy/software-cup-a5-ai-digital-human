# -*- coding: utf-8 -*-
"""一键启动AI数字人"""
import sys, os, subprocess, time, webbrowser, urllib.request

_backend_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'backend')


def _backend_python_command():
    """Select any installed stable Python environment that has backend deps."""
    candidates = []
    if sys.version_info.releaselevel == 'final':
        candidates.append([sys.executable])
    if os.name == 'nt':
        # The Windows launcher may default to a prerelease build. Try supported
        # stable installations instead of pinning every machine to one version.
        candidates.extend([['py', f'-3.{minor}'] for minor in range(13, 7, -1)])

    seen = set()
    dependency_check = 'import flask, requests, onnxruntime, chromadb'
    for command in candidates:
        key = tuple(command)
        if key in seen:
            continue
        seen.add(key)
        try:
            result = subprocess.run(
                command + ['-c', dependency_check],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
            )
            if result.returncode == 0:
                return command
        except (OSError, subprocess.TimeoutExpired):
            continue

    raise RuntimeError(
        'No compatible Python environment found. Install project dependencies '
        'with: python -m pip install -r backend/requirements.txt'
    )

def main():
    print('=== AI Digital Human ===')
    print('Starting backend...')

    backend_python = _backend_python_command()
    popen_options = {'cwd': _backend_dir}
    if os.name == 'nt':
        popen_options['creationflags'] = subprocess.CREATE_NEW_CONSOLE

    subprocess.Popen(
        backend_python + [os.path.join(_backend_dir, 'main.py')],
        **popen_options,
    )

    for i in range(30):
        try:
            urllib.request.urlopen('http://localhost:8088/api/v1/health', timeout=1)
            print('Backend ready!')
            break
        except Exception:
            if i % 5 == 0:
                print(f'Waiting for backend... ({i+1}s)')
            time.sleep(1)
    else:
        print('Backend failed to start, check console window for errors')
        sys.exit(1)

    print('Opening browser...')
    webbrowser.open('http://localhost:8088/')
    print('Done!')
    time.sleep(2)


if __name__ == '__main__':
    main()
