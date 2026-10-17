import io
import warnings
import zipfile
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from .errors import TaskError
from .models import Extraction, Source
from .storage import atomic_write, write_json

MAX_COVER_BYTES = 20 * 1024 * 1024


def cover_jpeg(data: bytes) -> bytes:
    if len(data) > MAX_COVER_BYTES:
        raise TaskError("image_failed", "封面文件超过 20 MiB")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                if image.width * image.height > 40_000_000:
                    raise ValueError("封面像素过多")
                image.thumbnail((2048, 2048))
                output = io.BytesIO()
                image.convert("RGB").save(output, "JPEG", quality=90)
                return output.getvalue()
    except (OSError, ValueError, UnidentifiedImageError,
            Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise TaskError("image_failed", f"封面不是有效的可导入图片：{exc}") from exc


def convert(source: Source, extraction: Extraction, names: list[str] | None = None) -> list[dict]:
    result = []
    for index, recipe in enumerate(extraction.recipes, 1):
        name = f"{recipe.name}（{source.author}·{source.note_id[-8:]}"
        name += f"·{index}）" if len(extraction.recipes) > 1 else "）"
        if names and len(names) == len(extraction.recipes):
            name = names[index - 1]
        notes = [f"作者：{source.author}", f"原视频：{source.url}"]
        notes.extend(recipe.notes)
        ingredients = []
        seen = set()
        for ingredient in recipe.ingredients:
            purpose = ingredient.purpose.strip()
            label = f"{ingredient.name}（{purpose}）" if purpose else ingredient.name
            # Avoid openCook silently merging repeated rows, including estimated amounts.
            base = label
            suffix = 2
            while (label.casefold(), ingredient.unit.casefold()) in seen:
                label = f"{base}（第{suffix}项）"
                suffix += 1
            seen.add((label.casefold(), ingredient.unit.casefold()))
            ingredients.append({
                "name": label, "quantity": ingredient.quantity, "unit": ingredient.unit,
            })
            amount = f"{ingredient.quantity:g}{ingredient.unit}"
            notes.append(f"原始用量｜{label}：{ingredient.original_amount}")
            if ingredient.estimated:
                notes.append(f"估算用量｜{label}：{amount}（原文：{ingredient.original_amount}）")
        item = {
            "@context": "https://schema.org", "@type": "Recipe", "name": name,
            "cookbook": source.author,
            "openCookIngredients": ingredients,
            "recipeInstructions": [{"@type": "HowToStep", "text": step} for step in recipe.steps],
            "openCookNotes": notes,
        }
        if recipe.description:
            item["description"] = recipe.description
        if recipe.servings is not None:
            item["openCookServings"] = recipe.servings
        for key, value in (("prepTime", recipe.prep_time), ("cookTime", recipe.cook_time),
                           ("totalTime", recipe.total_time)):
            if value:
                item[key] = value
        result.append(item)
    return result


def estimates(extraction: Extraction) -> list[dict]:
    return [
        {"recipe": recipe.name, **ingredient.model_dump()}
        for recipe in extraction.recipes
        for ingredient in recipe.ingredients if ingredient.estimated
    ]


def write_outputs(directory: Path, records: list[dict], report: dict):
    remote, bundled, images = [], [], {}
    for record in records:
        source = Source.model_validate(record["source"])
        extraction = Extraction.model_validate(record["extraction"])
        recipes = convert(source, extraction, record.get("export_names"))
        cover = record.get("cover_path")
        path = Path(cover) if cover else None
        image_name = f"images/{source.note_id}.jpg"
        if path and path.is_file():
            # Cached images are verified again, so HTML/corrupt files cannot enter a bundle.
            try:
                images[image_name] = cover_jpeg(path.read_bytes())
            except (TaskError, OSError) as exc:
                record["image_error"] = {"code": "image_failed", "message": str(exc)}
        elif cover:
            record["image_error"] = {"code": "image_missing", "message": "缓存封面文件已丢失"}
        for task in report.get("tasks", []):
            if (task.get("note_id") == source.note_id
                    and task.get("status") in {"success", "cached"}):
                task["image_error"] = record.get("image_error")
        for recipe in recipes:
            standalone = dict(recipe)
            zipped = dict(recipe)
            if source.cover_url:
                standalone["image"] = [source.cover_url]
            if image_name in images:
                zipped["image"] = [image_name]
            remote.append(standalone)
            bundled.append(zipped)
    directory.mkdir(parents=True, exist_ok=True)
    write_json(directory / "recipes.json", remote)
    import json

    content = io.BytesIO()
    with zipfile.ZipFile(content, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("recipes.json", json.dumps(bundled, ensure_ascii=False, indent=2))
        for name, data in images.items():
            archive.writestr(name, data)
    atomic_write(directory / "recipes.zip", content.getvalue())
    write_json(directory / "report.json", {
        **report, "recipe_count": len(remote), "verification": "format_and_reference_only",
        "content_notice": "自动检查仅验证格式及引用，不代表已核实视频内容或估算用量。",
    })
