"""ComfyUI's model folders, and which folder each loader node reads from.

Workflows save a model's path relative to its model folder, without the folder
itself: a LoraLoader that saved "h3\\whatever.safetensors" means
models/loras/h3/whatever.safetensors. The node type tells which folder that is.
Nodes not in NODE_CATEGORIES fall back to matching the path inside any of
ComfyUI's model folders.
"""

from __future__ import annotations

# Folder names ComfyUI itself defines inside the models folder. These are never
# treated as a single model and are never deleted as a whole.
STANDARD_CATEGORIES = frozenset({
    "audio_encoders", "background_removal", "checkpoints", "classifiers", "clip",
    "clip_vision", "configs", "controlnet", "custom_nodes", "datasets", "detection",
    "diffusers", "diffusion_models", "embeddings", "frame_interpolation",
    "geometry_estimation", "gligen", "hypernetworks", "latent_upscale_models",
    "loras", "model_patches", "optical_flow", "photomaker", "style_models",
    "t2i_adapter", "text_encoders", "unet", "upscale_models", "vae", "vae_approx",
})

# Folders ComfyUI merges into one category, plus the folder keys some popular
# custom nodes use for the same folders.
FOLDER_ALIASES = {
    "unet": "diffusion_models",
    "clip": "text_encoders",
    "t2i_adapter": "controlnet",
    "unet_gguf": "diffusion_models",
    "clip_gguf": "text_encoders",
    "vae_gguf": "vae",
}

# Node type -> model folder(s) it lists. Core entries come from ComfyUI's nodes.py
# and comfy_extras; the rest are widely used custom node packs.
_NODE_FOLDERS = {
    # ComfyUI core
    "AudioEncoderLoader": "audio_encoders",
    "CheckpointLoader": "checkpoints configs",
    "CheckpointLoaderSimple": "checkpoints",
    "CLIPLoader": "text_encoders",
    "CLIPVisionLoader": "clip_vision",
    "ControlNetLoader": "controlnet",
    "CreateHookLora": "loras",
    "CreateHookLoraModelOnly": "loras",
    "CreateHookModelAsLora": "checkpoints",
    "CreateHookModelAsLoraModelOnly": "checkpoints",
    "DiffControlNetLoader": "controlnet",
    "DualCLIPLoader": "text_encoders",
    "FrameInterpolationModelLoader": "frame_interpolation",
    "GLIGENLoader": "gligen",
    "HypernetworkLoader": "hypernetworks",
    "ImageOnlyCheckpointLoader": "checkpoints",
    "LatentUpscaleModelLoader": "latent_upscale_models",
    "LoadBackgroundRemovalModel": "background_removal",
    "LoadDA3Model": "geometry_estimation",
    "LoadMediaPipeFaceLandmarker": "detection",
    "LoadMoGeModel": "geometry_estimation",
    "LoraLoader": "loras",
    "LoraLoaderBypass": "loras",
    "LoraLoaderBypassModelOnly": "loras",
    "LoraLoaderModelOnly": "loras",
    "LTXAVTextEncoderLoader": "checkpoints text_encoders",
    "LTXVAudioVAELoader": "checkpoints",
    "ModelPatchLoader": "model_patches",
    "OpticalFlowLoader": "optical_flow",
    "PhotoMakerLoader": "photomaker",
    "QuadrupleCLIPLoader": "text_encoders",
    "SAM3DBody_Loader": "detection",
    "StyleModelLoader": "style_models",
    "TripleCLIPLoader": "text_encoders",
    "unCLIPCheckpointLoader": "checkpoints",
    "UNETLoader": "diffusion_models",
    "UpscaleModelLoader": "upscale_models",
    "VAELoader": "vae vae_approx",
    # ComfyUI-GGUF
    "UnetLoaderGGUF": "diffusion_models",
    "UnetLoaderGGUFAdvanced": "diffusion_models",
    "CLIPLoaderGGUF": "text_encoders",
    "DualCLIPLoaderGGUF": "text_encoders",
    "TripleCLIPLoaderGGUF": "text_encoders",
    "QuadrupleCLIPLoaderGGUF": "text_encoders",
    # ComfyUI-KJNodes
    "VAELoaderKJ": "vae vae_approx",
    "CheckpointLoaderKJ": "checkpoints",
    "DiffusionModelLoaderKJ": "diffusion_models",
    # ComfyUI-WanVideoWrapper
    "WanVideoModelLoader": "diffusion_models",
    "WanVideoVAELoader": "vae",
    "WanVideoTinyVAELoader": "vae_approx",
    "WanVideoLoraSelect": "loras",
    "WanVideoLoraSelectMulti": "loras",
    "WanVideoLoraSelectByName": "loras",
    "LoadWanVideoT5TextEncoder": "text_encoders",
    "LoadWanVideoClipTextEncoder": "clip_vision text_encoders",
    "WanVideoVACEModelSelect": "diffusion_models",
    "WanVideoExtraModelSelect": "diffusion_models",
    "WanVideoControlnetLoader": "controlnet",
    # rgthree-comfy
    "Power Lora Loader (rgthree)": "loras",
    "Lora Loader Stack (rgthree)": "loras",
    # ComfyUI-LTXVideo
    "LTXICLoRALoaderModelOnly": "loras",
    "LTX2LoraLoaderAdvanced": "loras",
    "LTXVGemmaCLIPModelLoader": "checkpoints text_encoders",
    "LowVRAMLatentUpscaleModelLoader": "latent_upscale_models",
}


# Secondary folders some loaders also list (VAELoader shows the taesd preview
# decoders, CheckpointLoader the .yaml configs). Ignored when deciding which
# folder a misplaced model belongs in.
AUX_CATEGORIES = frozenset({"vae_approx", "configs"})


def canonical_category(folder: str) -> str:
    folder = folder.strip().lower()
    return FOLDER_ALIASES.get(folder, folder)


NODE_CATEGORIES: dict[str, tuple[str, ...]] = {
    node: tuple(dict.fromkeys(canonical_category(f) for f in folders.split()))
    for node, folders in _NODE_FOLDERS.items()
}


def node_categories(node_type: str) -> tuple[str, ...] | None:
    """Model folders a node reads from, or None if the node is not known."""
    return NODE_CATEGORIES.get(node_type)


def target_category(categories: tuple[str, ...]) -> str | None:
    """The one folder a node's models belong in, or None if that is ambiguous."""
    primary = [c for c in categories if c not in AUX_CATEGORIES]
    return primary[0] if len(primary) == 1 else None
