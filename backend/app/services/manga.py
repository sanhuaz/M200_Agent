from __future__ import annotations

from pathlib import Path

import jmcomic


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
        if not pdfs:
            raise RuntimeError("JMComic 下载完成但没有找到 PDF 产物")
        return pdfs[0]


manga_service = MangaService()
