import base64
import io
import time
import uvicorn
import os
import uuid
from threading import Lock
from gradio.processing_utils import encode_pil_to_base64, decode_base64_to_file, decode_base64_to_image
from fastapi import APIRouter, Depends, FastAPI, HTTPException, BackgroundTasks
import modules.shared as shared
from modules.api.models import *
from modules.processing import StableDiffusionProcessingTxt2Img, StableDiffusionProcessingImg2Img, process_images
from modules.sd_samplers import all_samplers
from modules.extras import run_extras, run_pnginfo
from modules.sd_models import checkpoints_list, get_closest_checkpoint_match, reload_model_weights
from modules.realesrgan_model import get_realesrgan_models
from typing import List

def upscaler_to_index(name: str):
    try:
        return [x.name.lower() for x in shared.sd_upscalers].index(name.lower())
    except:
        raise HTTPException(status_code=400, detail=f"Invalid upscaler, needs to be on of these: {' , '.join([x.name for x in sd_upscalers])}")


sampler_to_index = lambda name: next(filter(lambda row: name.lower() == row[1].name.lower(), enumerate(all_samplers)), None)


def setUpscalers(req: dict):
    reqDict = vars(req)
    reqDict['extras_upscaler_1'] = upscaler_to_index(req.upscaler_1)
    reqDict['extras_upscaler_2'] = upscaler_to_index(req.upscaler_2)
    reqDict.pop('upscaler_1')
    reqDict.pop('upscaler_2')
    return reqDict


def encode_pil_to_base64(image):
    buffer = io.BytesIO()
    image.save(buffer, format="png")
    return base64.b64encode(buffer.getvalue())


class Api:
    def __init__(self, app: FastAPI, queue_lock: Lock):
        self.router = APIRouter()
        self.app = app
        self.queue_lock = queue_lock
        self.app.add_api_route("/sdapi/v1/txt2img", self.text2imgapi, methods=["POST"], response_model=TextToImageResponse)
        self.app.add_api_route("/sdapi/v1/img2img", self.img2imgapi, methods=["POST"], response_model=ImageToImageResponse)
        self.app.add_api_route("/sdapi/v1/txt2imgLab", self.text2imgapiLab, methods=["POST"], response_model=TextToImageLabResponse)
        self.app.add_api_route("/sdapi/v1/img2imgLab", self.img2imgapiLab, methods=["POST"], response_model=ImageToImageLabResponse)
        self.app.add_api_route("/sdapi/v1/extra-single-image", self.extras_single_image_api, methods=["POST"], response_model=ExtrasSingleImageResponse)
        self.app.add_api_route("/sdapi/v1/extra-batch-images", self.extras_batch_images_api, methods=["POST"], response_model=ExtrasBatchImagesResponse)
        self.app.add_api_route("/sdapi/v1/png-info", self.pnginfoapi, methods=["POST"], response_model=PNGInfoResponse)
        self.app.add_api_route("/sdapi/v1/progress", self.progressapi, methods=["GET"], response_model=ProgressResponse)
        self.app.add_api_route("/sdapi/v1/interrupt", self.interruptapi, methods=["POST"])
        self.app.add_api_route("/sdapi/v1/options", self.get_config, methods=["GET"], response_model=OptionsModel)
        self.app.add_api_route("/sdapi/v1/options", self.set_config, methods=["POST"])
        self.app.add_api_route("/sdapi/v1/cmd-flags", self.get_cmd_flags, methods=["GET"], response_model=FlagsModel)
        self.app.add_api_route("/sdapi/v1/samplers", self.get_samplers, methods=["GET"], response_model=List[SamplerItem])
        self.app.add_api_route("/sdapi/v1/upscalers", self.get_upscalers, methods=["GET"], response_model=List[UpscalerItem])
        self.app.add_api_route("/sdapi/v1/sd-models", self.get_sd_models, methods=["GET"], response_model=List[SDModelItem])
        self.app.add_api_route("/sdapi/v1/sd-models", self.set_sd_models, methods=["POST"])
        self.app.add_api_route("/sdapi/v1/hypernetworks", self.get_hypernetworks, methods=["GET"], response_model=List[HypernetworkItem])
        self.app.add_api_route("/sdapi/v1/face-restorers", self.get_face_restorers, methods=["GET"], response_model=List[FaceRestorerItem])
        self.app.add_api_route("/sdapi/v1/realesrgan-models", self.get_realesrgan_models, methods=["GET"], response_model=List[RealesrganItem])
        self.app.add_api_route("/sdapi/v1/prompt-styles", self.get_promp_styles, methods=["GET"], response_model=List[PromptStyleItem])
        self.app.add_api_route("/sdapi/v1/artist-categories", self.get_artists_categories, methods=["GET"], response_model=List[str])
        self.app.add_api_route("/sdapi/v1/artists", self.get_artists, methods=["GET"], response_model=List[ArtistItem])
    
    def text2imgapi(self, txt2imgreq: StableDiffusionTxt2ImgProcessingAPI):
        sampler_index = sampler_to_index(txt2imgreq.sampler_index)

        if sampler_index is None:
            raise HTTPException(status_code=404, detail="Sampler not found")

        populate = txt2imgreq.copy(update={ # Override __init__ params
            "sd_model": shared.sd_model,
            "sampler_index": sampler_index[0],
            "do_not_save_samples": True,
            "do_not_save_grid": True
            }
        )
        p = StableDiffusionProcessingTxt2Img(**vars(populate))
        # Override object param

        with self.queue_lock:
            processed = process_images(p)

        b64images = list(map(encode_pil_to_base64, processed.images))

        return TextToImageResponse(images=b64images, parameters=vars(txt2imgreq), info=processed.js())
    
    def text2imgapiLab(self, txt2imgreqLab: TextToImageLabRequest, background_tasks: BackgroundTasks):
        txt2imgreq = StableDiffusionTxt2ImgProcessingAPI()
        txt2imgreq.prompt = txt2imgreqLab.prompt
        if txt2imgreqLab.style:
            txt2imgreq.styles = [txt2imgreqLab.style]
        else:
            txt2imgreq.styles = []
            
        if "-concept" in txt2imgreqLab.style:
            txt2imgreq.override_settings = {"sd_model_checkpoint": f"{txt2imgreqLab.style}.ckpt"}
        else:
            txt2imgreq.override_settings = {"sd_model_checkpoint": f"v1-5-pruned-emaonly.ckpt"}
            
        txt2imgreq.cfg_scale = 11
        txt2imgreq.batch_size = 8
        txt2imgreq.steps = 50
        txt2imgreq.negative_prompt = ""
        txt2imgreq.sampler_index = "Euler a"
        
        if "sd_model_checkpoint" in txt2imgreq.override_settings:
            background_tasks.add_task(lambda: self.set_sd_models(LoadModelRequest(name=txt2imgreq.override_settings['sd_model_checkpoint'])))
        
        my_hash = str(uuid.uuid4())
        
        def temp():
            images = self.text2imgapi(txt2imgreq)

            if not os.path.exists(f'outputs/api_imgs/'):
                os.makedirs(f'outputs/api_imgs/')
                
            with open(f'outputs/api_imgs/{my_hash}.txt', "w") as fp:
                fp.write('\n'.join(images.images))
                
            shared.state.end()

        shared.state.begin(job_name=my_hash)
        background_tasks.add_task(temp)
        return TextToImageLabResponse(job_hash=my_hash, job_no=shared.state.job_count-1, job_count=shared.state.job_count)

    def img2imgapi(self, img2imgreq: StableDiffusionImg2ImgProcessingAPI):
        sampler_index = sampler_to_index(img2imgreq.sampler_index)

        if sampler_index is None:
            raise HTTPException(status_code=404, detail="Sampler not found")


        init_images = img2imgreq.init_images
        if init_images is None:
            raise HTTPException(status_code=404, detail="Init image not found")

        mask = img2imgreq.mask
        if mask:
            mask = decode_base64_to_image(mask)


        populate = img2imgreq.copy(update={ # Override __init__ params
            "sd_model": shared.sd_model,
            "sampler_index": sampler_index[0],
            "do_not_save_samples": True,
            "do_not_save_grid": True,
            "mask": mask
            }
        )
        p = StableDiffusionProcessingImg2Img(**vars(populate))

        imgs = []
        for img in init_images:
            img = decode_base64_to_image(img)
            imgs = [img] * p.batch_size

        p.init_images = imgs

        with self.queue_lock:
            processed = process_images(p)

        b64images = list(map(encode_pil_to_base64, processed.images))

        if (not img2imgreq.include_init_images):
            img2imgreq.init_images = None
            img2imgreq.mask = None

        return ImageToImageResponse(images=b64images, parameters=vars(img2imgreq), info=processed.js())
    
    def img2imgapiLab(self, img2imgreqLab: ImageToImageLabRequest, background_tasks: BackgroundTasks):
        img2imgreq = StableDiffusionImg2ImgProcessingAPI()
        img2imgreq.prompt = img2imgreqLab.prompt
        if img2imgreqLab.style:
            img2imgreq.styles = [img2imgreqLab.style]
        else:
            img2imgreq.styles = []
            
        if "-concept" in img2imgreqLab.style:
            img2imgreq.override_settings = {"sd_model_checkpoint": f"{img2imgreqLab.style}.ckpt"}
        else:
            img2imgreq.override_settings = {"sd_model_checkpoint": f"v1-5-pruned-emaonly.ckpt"}
            
        img2imgreq.cfg_scale = 17
        img2imgreq.batch_size = 8
        img2imgreq.steps = 50
        img2imgreq.negative_prompt = ""
        img2imgreq.sampler_index = "Euler a"
        img2imgreq.denoising_strength = 0.75
        img2imgreq.subseed = -1
        img2imgreq.subseed_strength = 1
        img2imgreq.init_images = [';,' + img2imgreqLab.image]
        
        if "sd_model_checkpoint" in img2imgreq.override_settings:
            background_tasks.add_task(lambda: self.set_sd_models(LoadModelRequest(name=img2imgreq.override_settings['sd_model_checkpoint'])))
        
        my_hash = str(uuid.uuid4())
        
        def temp():
            images = self.img2imgapi(img2imgreq)

            if not os.path.exists(f'outputs/api_imgs/'):
                os.makedirs(f'outputs/api_imgs/')
                
            with open(f'outputs/api_imgs/{my_hash}.txt', "w") as fp:
                fp.write('\n'.join(images.images))
                
            shared.state.end()

        shared.state.begin(job_name=my_hash)
        background_tasks.add_task(temp)
        return ImageToImageLabResponse(job_hash=my_hash, job_no=shared.state.job_no, job_count=shared.state.job_count)

    def extras_single_image_api(self, req: ExtrasSingleImageRequest):
        reqDict = setUpscalers(req)

        reqDict['image'] = decode_base64_to_image(reqDict['image'])

        with self.queue_lock:
            result = run_extras(extras_mode=0, image_folder="", input_dir="", output_dir="", **reqDict)

        return ExtrasSingleImageResponse(image=encode_pil_to_base64(result[0][0]), html_info=result[1])

    def extras_batch_images_api(self, req: ExtrasBatchImagesRequest):
        reqDict = setUpscalers(req)

        def prepareFiles(file):
            file = decode_base64_to_file(file.data, file_path=file.name)
            file.orig_name = file.name
            return file

        reqDict['image_folder'] = list(map(prepareFiles, reqDict['imageList']))
        reqDict.pop('imageList')

        with self.queue_lock:
            result = run_extras(extras_mode=1, image="", input_dir="", output_dir="", **reqDict)

        return ExtrasBatchImagesResponse(images=list(map(encode_pil_to_base64, result[0])), html_info=result[1])

    def pnginfoapi(self, req: PNGInfoRequest):
        if(not req.image.strip()):
            return PNGInfoResponse(info="")

        result = run_pnginfo(decode_base64_to_image(req.image.strip()))

        return PNGInfoResponse(info=result[1])

    def progressapi(self, req: ProgressRequest = Depends()):
        # copy from check_progress_call of ui.py

        if shared.state.job_count == 0:
            return ProgressResponse(progress=0, eta_relative=0, state=shared.state.dict())

        # avoid dividing zero
        progress = 0.01

        if shared.state.job_count > 0:
            progress += shared.state.job_no / shared.state.job_count
        if shared.state.sampling_steps > 0:
            progress += 1 / shared.state.job_count * shared.state.sampling_step / shared.state.sampling_steps

        time_since_start = time.time() - shared.state.time_start
        if req.job_no and req.job_no != -1:
            end_point = (req.job_no + 1) / shared.state.job_count
            progress = progress / end_point
            
        eta = time_since_start / progress
        eta_relative = max(0, eta-time_since_start)

        progress = min(progress, 1)

        shared.state.set_current_image()

        current_image = None
        if shared.state.current_image and not req.skip_current_image:
            current_image = encode_pil_to_base64(shared.state.current_image)

        return ProgressResponse(progress=progress, eta_relative=eta_relative, state=shared.state.dict(), current_image=current_image)

    def interruptapi(self):
        shared.state.interrupt()

        return {}
        
    def get_config(self):
        options = {}
        for key in shared.opts.data.keys():
            metadata = shared.opts.data_labels.get(key)
            if(metadata is not None):
                options.update({key: shared.opts.data.get(key, shared.opts.data_labels.get(key).default)})
            else:
                options.update({key: shared.opts.data.get(key, None)})
        
        return options
        
    def set_config(self, req: OptionsModel):
        # currently req has all options fields even if you send a dict like { "send_seed": false }, which means it will
        # overwrite all options with default values.
        raise RuntimeError('Setting options via API is not supported')

        reqDict = vars(req)
        for o in reqDict:
            setattr(shared.opts, o, reqDict[o])

        shared.opts.save(shared.config_filename)
        return

    def get_cmd_flags(self):
        return vars(shared.cmd_opts)

    def get_samplers(self):
        return [{"name":sampler[0], "aliases":sampler[2], "options":sampler[3]} for sampler in all_samplers]

    def get_upscalers(self):
        upscalers = []
        
        for upscaler in shared.sd_upscalers:
            u = upscaler.scaler
            upscalers.append({"name":u.name, "model_name":u.model_name, "model_path":u.model_path, "model_url":u.model_url})
        
        return upscalers
        
    def get_sd_models(self):
        return [{"title":x.title, "model_name":x.model_name, "hash":x.hash, "filename": x.filename, "config": x.config} for x in checkpoints_list.values()]

    def set_sd_models(self, req: LoadModelRequest):
        name = req.name

        info = get_closest_checkpoint_match(name)
        if info is None:
            raise HTTPException(status_code=404, detail="Checkpoint not found")

        # shared.state.begin()
        with self.queue_lock:
            reload_model_weights(shared.sd_model, info)

        # shared.state.end()

        return "OK"
    
    def get_hypernetworks(self):
        return [{"name": name, "path": shared.hypernetworks[name]} for name in shared.hypernetworks]

    def get_face_restorers(self):
        return [{"name":x.name(), "cmd_dir": getattr(x, "cmd_dir", None)} for x in shared.face_restorers]

    def get_realesrgan_models(self):
        return [{"name":x.name,"path":x.data_path, "scale":x.scale} for x in get_realesrgan_models(None)]
    
    def get_promp_styles(self):
        styleList = []
        for k in shared.prompt_styles.styles:
            style = shared.prompt_styles.styles[k] 
            styleList.append({"name":style[0], "prompt": style[1], "negative_prompr": style[2]})

        return styleList

    def get_artists_categories(self):
        return shared.artist_db.cats

    def get_artists(self):
        return [{"name":x[0], "score":x[1], "category":x[2]} for x in shared.artist_db.artists]

    def launch(self, server_name, port):
        self.app.include_router(self.router)
        uvicorn.run(self.app, host=server_name, port=port)
