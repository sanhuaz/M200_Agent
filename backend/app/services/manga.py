from __future__ import annotations

from pathlib import Path

import jmcomic
from PIL import Image

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


class MangaService:
    def search(self, query: str, limit: int = 10) -> list[dict[str, object]]:
        option = jmcomic.JmOption.default()
        client = option.new_jm_client()
        page = client.search_site(query)
        results: list[dict[str, object]] = []
        for album_id, title, tags in page.iter_id_title_tag():
            results.append({"album_id": str(album_id), "title": title, "tags": list(tags or [])})
            if len(results) >= limit:
                break
        return results

    async def download_pdf(self, album_id: str, output_dir: Path) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        option = jmcomic.JmOption.construct(
            {
                "dir_rule": {"rule": "Bd_Pname", "base_dir": str(output_dir)},
                "download": {"cache": True},
            }
        )
        await jmcomic.download_album_async(
            album_id,
            option=option,
            extra=jmcomic.Feature.export_pdf,
        )
        pdfs = sorted(output_dir.rglob("*.pdf"), key=lambda path: path.stat().st_mtime, reverse=True)
        if pdfs:
            return pdfs[0]
        return self.export_downloaded_images(output_dir, album_id)

    @staticmethod
    def export_downloaded_images(output_dir: Path, album_id: str) -> Path:
        image_paths = sorted(
            (
                path
                for path in output_dir.rglob("*")
                if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
            ),
            key=lambda path: tuple(part.casefold() for part in path.relative_to(output_dir).parts),
        )
        if not image_paths:
            raise RuntimeError("JMComic 下载完成但没有找到图片或 PDF 产物")

        pages: list[Image.Image] = []
        try:
            for image_path in image_paths:
                with Image.open(image_path) as source:
                    pages.append(source.convert("RGB"))

            pdf_path = output_dir / f"JM{album_id}.pdf"
            pages[0].save(
                pdf_path,
                "PDF",
                save_all=True,
                append_images=pages[1:],
                resolution=100.0,
            )
        finally:
            for page in pages:
                page.close()

        if not pdf_path.is_file() or pdf_path.stat().st_size == 0:
            raise RuntimeError("JMComic 图片下载成功但 PDF 兜底导出失败")
        return pdf_path


manga_service = MangaService()
