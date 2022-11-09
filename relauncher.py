import os, time

n = 0
gradio_auth = os.getenv('GRADIO_AUTH')
while True:
    print('Relauncher: Launching...')
    if n > 0:
        print(f'\tRelaunch count: {n}')
        
    # Check if libraries have been installed
    if not os.path.isdir('/workspace/stable-diffusion-webui/repositories'):
        os.system("python launch.py --exit")
        
    launch_string = "python webui.py --api --port 3000 --ckpt /workspace/stable-diffusion-webui/models/Stable-diffusion/v1-5-pruned-emaonly.ckpt --opt-split-attention --listen --xformers"
    if gradio_auth:
        launch_string += " --gradio-auth " + gradio_auth
    os.system(launch_string)
    print('Relauncher: Process is ending. Relaunching in 2s...')
    n += 1
    time.sleep(2)
