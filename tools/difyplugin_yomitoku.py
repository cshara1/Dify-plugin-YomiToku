import asyncio
import csv
import io
import json
import os
from typing import Any
import logging
from pathlib import Path
from typing import Any, Dict, Generator

import pypdfium2
import torch

from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage
from dify_plugin.file.file import File

from yomitoku import DocumentAnalyzer
from yomitoku.data.functions import validate_image
from yomitoku.export import convert_json, convert_markdown, convert_csv, convert_html

from PIL import Image
import numpy as np


logger = logging.getLogger(__name__)

class DifypluginYomitokuTool(Tool):
    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage, None, None]:
        yield from self._parser_file(tool_parameters)


    def _load_config(self, tool_parameters: dict[str, Any]) -> None:
        configs = {
            "ocr": {
                "text_detector": {
                    "path_cfg": None,
                },
                "text_recognizer": {
                    "path_cfg": None,
                },
            },
            "layout_analyzer": {
                "layout_parser": {
                    "path_cfg": None,
                },
                "table_structure_recognizer": {
                    "path_cfg": None,
                },
            },
        }

        if tool_parameters.get("lite"):
            configs["ocr"]["text_recognizer"]["model_name"] = "parseq-small"

            if tool_parameters.get("cpu") or not torch.cuda.is_available():
                configs["ocr"]["text_detector"]["infer_onnx"] = True

        return configs

    def _load_pdf(self, file, dpi=200) -> list[np.ndarray]:
        """
        Open a PDF file.
        """
        try:
            file_bytes = file.blob
            
            doc = pypdfium2.PdfDocument(file_bytes)
            renderer = doc.render(
                pypdfium2.PdfBitmap.to_pil,
                scale=dpi / 72,
            )
            images = list(renderer)
            images = [np.array(image.convert("RGB"))[:, :, ::-1] for image in images]

            doc.close()
        except Exception as e:
            logger.error(f"Failed to open the PDF file: {file.filename}, {e}")
            raise ValueError(f"Failed to open the PDF file: {file.filename}, {e}") from e

        return images

    def _load_image(self, file) -> np.ndarray:
        """
        Open an image file.

        Returns:
            np.ndarray: image data(BGR)
        """

        try:
            file_bytes = file.blob
            img = Image.open(io.BytesIO(file_bytes))
        except Exception:
            raise ValueError("Invalid image data.")

        extension = os.path.splitext(file.filename)[1].lower()
        pages = []
        if extension in [".tif", ".tiff"]:
            try:
                while True:
                    img_arr = np.array(img.copy().convert("RGB"))
                    validate_image(img_arr)
                    pages.append(img_arr[:, :, ::-1])
                    img.seek(img.tell() + 1)
            except EOFError:
                pass
        else:
            img_arr = np.array(img.convert("RGB"))
            validate_image(img_arr)
            pages.append(img_arr[:, :, ::-1])

        return pages

    def _parser_file(self, tool_parameters: Dict[str, Any]):
        """Parse files by local server."""
        file = tool_parameters.get("file")
        output_format = tool_parameters.get("output_format", "json")
        configs = self._load_config(tool_parameters)
        ignore_meta = tool_parameters.get("ignore_meta")
        figure_letter = tool_parameters.get("figure_letter")
        ignore_line_break = tool_parameters.get("ignore_line_break")
        reading_order = tool_parameters.get("reading_order")
        
        if not file:
            logger.error("No file provided for file parsing")
            raise ValueError("File is required")

        self._validate_file_type(file.filename)
        logger.info("initializing analyzer")
        device = "cuda" if torch.cuda.is_available() else "cpu"

        analyzer = DocumentAnalyzer(
            configs=configs,
            visualize=False,
            device=device,
            ignore_meta=ignore_meta,
            reading_order=reading_order,)
        
        extension = os.path.splitext(file.filename)[1].lower()

        if extension == ".pdf":
            imgs = self._load_pdf(file)
        else:
            imgs = self._load_image(file)

        logger.info("parsing file")
        results = []

        try:
            for page, img in enumerate(imgs):
                analyzer.img = img
                result, _, _ = asyncio.run(analyzer.run(img))
                results.append(result)

        except Exception as e:
            logger.error(f"Failed analize file: {e}")
            raise ValueError(f"Failed analize file: {e}") from e

        if output_format == "json":
            yield self.create_text_message(json.dumps(
                [
                    convert_json(
                        result,
                        out_path=None,
                        ignore_line_break=ignore_line_break,
                        img=img,
                        export_figure=False,
                        # export_figure_letter=figure_letter,
                        figure_dir=None,
                    ).model_dump()
                    for img, result in zip(imgs, results)
                ],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ": "),
            ))
        elif output_format == "markdown":
            yield self.create_text_message("\n".join(
                [
                    convert_markdown(
                        result,
                        out_path=None,
                        ignore_line_break=ignore_line_break,
                        img=img,
                        export_figure=False,
                        export_figure_letter=figure_letter,

                    )[0]
                    for img, result in zip(imgs, results)
                ]
            ))
        elif output_format == "html":
            yield self.create_text_message("\n".join(
                [
                    convert_html(
                        result,
                        out_path=None,
                        ignore_line_break=ignore_line_break,
                        img=img,
                        export_figure=False,
                        export_figure_letter=figure_letter,
                    )[0]
                    for img, result in zip(imgs, results)
                ]
            ))
        elif output_format == "csv":
            output = io.StringIO()
            writer = csv.writer(output, quoting=csv.QUOTE_MINIMAL)
            for img, result in zip(imgs, results):
                elements = convert_csv(
                    result,
                    out_path=None,
                    ignore_line_break=ignore_line_break,
                    img=img,
                    export_figure=False,
                    # export_figure_letter=figure_letter,

                )
                for element in elements:
                    if element["type"] == "table":
                        writer.writerows(element["element"])
                    else:
                        writer.writerow([element["element"]])
                    writer.writerow([""])
            yield self.create_text_message(output.getvalue())
        else:
            logger.error(f"Unsupported output format: {output_format}."
                " Supported formats are json, markdown, html or csv.")
            raise ValueError(
                f"Unsupported output format: {output_format}."
                " Supported formats are json, markdown, html or csv."
            )


    @staticmethod
    def _validate_file_type(filename: str) -> str:
        extension = os.path.splitext(filename)[1].lower()
        if extension not in [".pdf",  ".png", ".jpg", ".jpeg", '.png', '.webp', '.bmp', '.tiff', '.tif', '.gif', ]:
            raise ValueError(f"File extension {extension} is not supported")
        return extension



