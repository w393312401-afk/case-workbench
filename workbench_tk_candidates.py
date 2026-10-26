"""Automatic TK-card catalog membership and recoverable production links."""
from __future__ import annotations

from creation_store import CreationError
from direction_store import DirectionError, product_key
from elevenlabs_client import VoiceError
from tk_candidates import CandidateError, CandidateStore


class TKCandidateWorkbenchMixin:
    def _associate_tk_candidate(self, identifier):
        """Register supplied card facts, then associate its exact saved records.

        Catalog registration and both record writes are idempotent. A completion
        marker goes last so a restart can finish an interrupted association.
        Known missing facts remain visible on the card without blocking editing.
        """
        with self.lock, self.store.index_snapshot():
            item = self.tk_candidates.get(identifier)
            if item["status"] == "dismissed":
                return item
            registered = None
            try:
                self.synced_directions(self.store.read_index()["cases"])
                registered = self.directions.register_candidate(item)
                direction_id = registered["direction"]["direction_id"]
                ref = registered["variant_ref"]
                product = product_key(item["product"])
                script_id = item.get("script_id") or ""
                job_id = item.get("job_id") or ""
                if job_id:
                    job = self.production.job(job_id)
                    if product_key(job["product"]) != product:
                        raise CandidateError(409, "候选与原制作单的产品不同，未更换原关联。")
                    if script_id and job.get("script_id") != script_id:
                        raise CandidateError(409, "候选保存的脚本与制作单采用稿不同，未更换原关联。")
                    script_id = script_id or job.get("script_id") or ""
                    if not script_id:
                        raise CandidateError(409, "原制作单没有采用稿编号，暂时无法补齐关联。")
                    links = [row for row in self.creation.read()["variant_links"]
                             if row.get("kind") == "production" and row.get("record_id") == job_id]
                    if len(links) > 1:
                        raise CandidateError(409, "原制作单存在重复变体关联，请先核对原记录。")
                    if links and (product_key(links[0].get("product")) != product
                                  or links[0].get("direction_id") != direction_id
                                  or not self.creation._same_variant_identity(links[0].get("variant_ref"), ref)):
                        raise CandidateError(409, "原制作单已有其他方向或变体的明确关联，未覆盖原关联。")
                script_args = dict(script_id=script_id, product=product, direction_id=direction_id,
                                   variant_ref=ref, reference_url=item["reference"]["url"])
                job_args = dict(job_id=job_id, script_id=script_id, product=product,
                                direction_id=direction_id, variant_ref=ref)
                # Validate both targets before writing either one. Source/version
                # snapshots, script words, takes and timelines are never replaced.
                if script_id:
                    self.creation.associate_candidate(identifier, **script_args, dry_run=True)
                if job_id:
                    self.production.associate_candidate(identifier, **job_args, dry_run=True)
                if script_id:
                    self.creation.associate_candidate(identifier, **script_args)
                if job_id:
                    self.production.associate_candidate(identifier, **job_args)
                if script_id and script_id != item.get("script_id"):
                    self.tk_candidates.link(identifier, script_id=script_id)
                result = self.tk_candidates.remember_direction(identifier, direction_id=direction_id,
                                                               variant_ref=ref, complete=True)
                self.state_cache = None
                return result
            except (CandidateError, DirectionError, CreationError, VoiceError) as exc:
                fields = ({"direction_id": registered["direction"]["direction_id"],
                           "variant_ref": registered["variant_ref"]} if registered else {})
                return self.tk_candidates.remember_direction(identifier, error=str(exc), **fields)

    def reconcile_tk_candidates(self, product=None):
        """Upgrade existing cards once at startup; safe to resume after recovery."""
        candidates = self.tk_candidates.list(product)
        if not candidates:
            return {"associated": 0, "errors": []}
        associated, errors = 0, []
        for item in candidates:
            if item["status"] == "dismissed":
                continue
            result = self._associate_tk_candidate(item["candidate_id"])
            if result.get("direction_error"):
                errors.append({"candidate_id": item["candidate_id"], "error": result["direction_error"]})
            else:
                associated += 1
        return {"associated": associated, "errors": errors}

    def tk_candidate_associate(self, body):
        if (not isinstance(body, dict) or set(body) != {"candidate_id"}
                or not isinstance(body.get("candidate_id"), str)):
            raise CandidateError(400, "候选方向关联参数格式无效。")
        item = self._associate_tk_candidate(body["candidate_id"])
        if item.get("direction_error"):
            raise CandidateError(409, item["direction_error"])
        item = self._auto_produce_tk_candidate(item)
        return {"candidate": CandidateStore.brief(item)}
