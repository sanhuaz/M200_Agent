from __future__ import annotations

from contextvars import ContextVar
from pathlib import Path

import jmcomic
from jmcomic.jm_plugin import AsyncProgressDownloader
from PIL import Image

from app.services.operation_logs import operation_logs

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
_download_context: ContextVar[dict[str, object] | None] = ContextVar("manga_download_context", default=None)


class OperationProgressDownloader(AsyncProgressDownloader):
    """Reuse JMComic's real album/chapter/image callbacks and mirror progress to M200."""

    def _ctx(self) -> dict[str, object] | None:
        return _download_context.get()

    def _emit(
        self, title: str, message: str, progress: dict[str, object], details: dict[str, object]
    ) -> None:
        ctx = self._ctx()
        if ctx and ctx.get("operation_id"):
            operation_logs.update_operation(
                str(ctx["operation_id"]),
                source="download",
                title=title,
                message=message,
                progress=progress,
                details={**details, "album_id": ctx.get("album_id")},
            )

    async def before_album(self, album):
        await super().before_album(album)
        total = len(album)
        self._emit(
            "漫画下载",
            f"已读取漫画章节，共 {total} 章",
            {"current": 0, "total": total, "percent": 0, "unit": "章"},
            {"stage": "album"},
        )

    async def before_photo(self, photo):
        await super().before_photo(photo)
        total_images = sum(self.chapter_total.values())
        self._emit(
            "章节下载",
            f"开始下载章节 {photo.id}，共 {len(photo)} 张图片",
            {
                "current": sum(self.chapter_done.values()),
                "total": total_images,
                "percent": round(sum(self.chapter_done.values()) / total_images * 100, 1)
                if total_images
                else 0,
                "unit": "张",
            },
            {"stage": "chapter", "photo_id": str(photo.id), "chapter_total": len(photo)},
        )

    async def after_image(self, image, img_save_path):
        await super().after_image(image, img_save_path)
        total = sum(self.chapter_total.values())
        current = sum(self.chapter_done.values())
        self._emit(
            "漫画下载进度",
            f"已下载 {current}/{total} 张图片",
            {
                "current": current,
                "total": total,
                "percent": round(current / total * 100, 1) if total else 0,
                "unit": "张",
            },
            {"stage": "image", "path": str(img_save_path)},
        )

    async def after_photo(self, photo):
        await super().after_photo(photo)
        total = len(self.chapter_total)
        current = self.album_done
        self._emit(
            "章节完成",
            f"已完成 {current}/{total} 个章节",
            {
                "current": current,
                "total": total,
                "percent": round(current / total * 100, 1) if total else 0,
                "unit": "章",
            },
            {"stage": "chapter_finished", "photo_id": str(photo.id)},
        )

    async def after_album(self, album):
        await super().after_album(album)
        self._emit(
            "漫画下载完成",
            "图片下载已完成，准备生成 PDF",
            {"current": 100, "total": 100, "percent": 100, "unit": "%"},
            {"stage": "album_finished"},
        )


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

    async def download_pdf(self, album_id: str, output_dir: Path, operation_id: str | None = None) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        option = jmcomic.JmOption.construct(
            {
                "dir_rule": {"rule": "Bd_Pname", "base_dir": str(output_dir)},
                "download": {"cache": True},
            }
        )
        context_token = _download_context.set({"operation_id": operation_id, "album_id": album_id})
        try:
            await jmcomic.download_album_async(
                album_id,
                option=option,
                downloader=OperationProgressDownloader,
                extra=jmcomic.Feature.export_pdf,
            )
        finally:
            _download_context.reset(context_token)
        pdfs = sorted(output_dir.rglob("*.pdf"), key=lambda path: path.stat().st_mtime, reverse=True)
        if pdfs:
            if operation_id:
                operation_logs.emit(
                    source="download",
                    kind="succeeded",
                    title="PDF 生成完成",
                    message="JMComic 已生成 PDF 产物",
                    operation_id=operation_id,
                    details={"path": str(pdfs[0].resolve()), "stage": "pdf"},
                )
            return pdfs[0]
        path = self.export_downloaded_images(output_dir, album_id)
        if operation_id:
            operation_logs.emit(
                source="download",
                kind="succeeded",
                title="PDF 生成完成",
                message="已由图片导出 PDF 产物",
                operation_id=operation_id,
                details={"path": str(path.resolve()), "stage": "pdf_fallback"},
            )
        return path

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
