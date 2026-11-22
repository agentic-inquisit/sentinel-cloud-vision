"""Build step: download every pretrained model into the image and run one
inference on each, so a broken model fails the build instead of the deploy.
 
"""

from PIL import Image

from catalog import SPECS

image = Image.new("RGB", (224, 224), (127, 127, 127))
SMOKE = {
    "image-classification": lambda m: m.predict(image, top_k=1),
    "object-detection": lambda m: m.predict(image),
    "image-text-matching": lambda m: m.predict(image, ["a grey square", "a dog"]),
    "fill-mask": lambda m: m.predict("Paris is the [MASK] of France.", top_k=1),
    "text-generation": lambda m: m.predict("Hello", max_new_tokens=3),
}

for spec in SPECS:
    if spec.task not in SMOKE:
        print(f"skip {spec.name}: no public weights")
        continue
    model = spec.load()
    print(f"ok   {spec.name}: {SMOKE[spec.task](model)}")
    del model
