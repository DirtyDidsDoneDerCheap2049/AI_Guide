"""City/place browsing: shared short-lived cache, bounded provider calls."""
import hashlib
import json
from datetime import datetime, timezone

import redis
from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import get_current_session, get_settings_dep
from app.config import Settings
from app.models import DemoSession
from app.agent.providers import build_provider_set
from app.agent.providers.base import ProviderError, ProviderNoResult
from app.services.discovery import CITY_STARTERS, provider_photos, resources_for

router = APIRouter()


@router.get('/discovery')
def discover(city: str = Query(min_length=1, max_length=60), q: str = Query(default='', max_length=100),
             session: DemoSession = Depends(get_current_session), settings: Settings = Depends(get_settings_dep)):
    city, q = city.strip(), q.strip()
    if not city:
        raise HTTPException(422, detail={"code": "city_required"})
    key = 'discovery:v2:' + hashlib.sha256(f'{settings.provider_mode}|{city}|{q}'.encode()).hexdigest()
    cache = redis.Redis.from_url(settings.redis_url, socket_timeout=2, socket_connect_timeout=2, decode_responses=True)
    try:
        cached = cache.get(key)
        if cached:
            return {**json.loads(cached), "cached": True}
        for bucket, limit in ((f'discovery:session:{session.id}', 20), ('discovery:global', 60)):
            count = cache.eval("local n=redis.call('INCR',KEYS[1]); if n==1 then redis.call('EXPIRE',KEYS[1],60) end; return n", 1, bucket)
            if count > limit:
                raise HTTPException(429, detail={"code": "discovery_rate_limited"})
    except redis.RedisError:
        raise HTTPException(503, detail={"code": "discovery_unavailable"}) from None
    finally:
        cache.close()
    providers = build_provider_set(settings.model_copy(update={'provider_timeout_seconds': min(8.0, settings.provider_timeout_seconds)}))
    result = {"city": city, "query": q, "places": [], "resources": resources_for(city, q), "fetched_at": datetime.now(timezone.utc).isoformat(), "cached": False, "fixture": providers.mode == 'mock', "error": None}
    queries = [q] if q else CITY_STARTERS.get(city.removesuffix('市'), ['风景名胜'])
    for keyword in queries:
        try:
            found = providers.place.search(name=keyword, city_hint=city, limit=1 if len(queries) > 1 else 6)
            result['places'].extend({"name": p.name, "address": p.address, "region": p.region, "longitude": p.longitude, "latitude": p.latitude, "provider": p.provider, "provider_place_id": p.provider_place_id, "fixture": providers.mode == 'mock', "photos": provider_photos(p.raw)} for p in found.candidates)
        except ProviderNoResult:
            continue
        except (ProviderError, ValueError, TypeError, AttributeError):
            result['error'] = '部分地点图片暂时没能加载，可以重试，或先查看下面的攻略和视频。'
            break
    # Error results are not cached. A failure never turns into fabricated photos.
    if not result['error']:
        cache = redis.Redis.from_url(settings.redis_url, socket_timeout=2, socket_connect_timeout=2)
        try:
            cache.set(key, json.dumps(result, ensure_ascii=False), ex=600)
        except redis.RedisError:
            pass
        finally:
            cache.close()
    return result
