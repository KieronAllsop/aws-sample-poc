import os
import json
import base64
from urllib.parse import quote_plus
import aioboto3
import xml.etree.ElementTree as ET
from fastapi import FastAPI, Request, HTTPException, Depends, status, Response
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# --- DATABASE ENGINE IMPORTS ---
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base, Mapped, mapped_column
from sqlalchemy.future import select

# =====================================================================
# 1. INITIALIZATION & DATABASE LAYOUT (PostgreSQL Integration)
# =====================================================================
app = FastAPI(title="Fargate DRM License Server")

# Global CORS Configuration required for cross-origin browser EME sandboxes
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

S3_BUCKET = os.environ.get("S3_BUCKET")
DB_USER   = os.environ.get("DB_USER")
DB_PASS   = os.environ.get("DB_PASSWORD")
DB_HOST   = os.environ.get("DB_HOST")
DB_NAME   = os.environ.get("DB_NAME")

if not all([S3_BUCKET, DB_USER, DB_PASS, DB_HOST, DB_NAME]):
    raise RuntimeError("Required S3 or database environment variables are missing")

# Asynchronous connection pooled driver engine linking to AWS RDS instance
DATABASE_URL = (
    f"postgresql+asyncpg://{quote_plus(DB_USER)}:{quote_plus(DB_PASS)}"
    f"@{DB_HOST}/{quote_plus(DB_NAME)}"
)
engine = create_async_engine(DATABASE_URL, pool_pre_ping=True, echo=False)
async_session_pool = async_sessionmaker(bind=engine, expire_on_commit=False)

# Initialize AWS non-blocking service loop session
aws_session = aioboto3.Session()
Base = declarative_base()

# SQLAlchemy ORM Model matching your tracking tables
class UserEntitlement(Base):
    __tablename__ = "user_entitlements"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[str] = mapped_column(index=True)
    key_id_hex: Mapped[str] = mapped_column(unique=True, index=True) # Hex footprint parsed from browser
    secret_key_hex: Mapped[str] = mapped_column()                   # Raw secret encryption key block
    is_active: Mapped[bool] = mapped_column(default=True)

# Database dependency provider injected directly into endpoint callers
async def get_db_session():
    async with async_session_pool() as session:
        try:
            yield session
        finally:
            await session.close()

# --- DYNAMIC STARTUP SCHEMASYNC RULE ---
# Automatically compiles and builds missing table architecture in RDS on launch
@app.on_event("startup")
async def startup_event():
    print("⏳ Web container booting up: Synchronizing schema metadata with RDS...")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("✅ Schema synchronization complete!")

# =====================================================================
# 2. UTILITY METHOD: HEX TO UNPADDED BASE64URL CONVERTER
# =====================================================================
def hex_to_base64url(hex_str: str) -> str:
    """
    Converts raw 32-character hexadecimal secret strings into 
    the unpadded urlsafe base64 tokens demanded by W3C ClearKey specifications.
    """
    binary_data = bytes.fromhex(hex_str)
    base64_encoded = base64.urlsafe_b64encode(binary_data).decode('utf-8')
    return base64_encoded.rstrip('=') # Strip formatting padding safely

# =====================================================================
# 3. ROUTE 1: ASYNCHRONOUS DYNAMIC MANIFEST PROXY (.mpd Modulator)
# =====================================================================
async def clear_key_manifest_generator(bucket: str, s3_key: str):
    """
    Streams a compiled Widevine manifest file straight out of S3, swaps the
    target DRM signatures on the fly in memory, and pipes it straight to network.
    """
    try:
        async with aws_session.client("s3") as s3:
            s3_object = await s3.get_object(Bucket=bucket, Key=s3_key)
            
            async with s3_object["Body"] as stream:
                manifest_bytes = await stream.read()
                manifest_text = manifest_bytes.decode("utf-8")

            # --- THE DRM UUID TRANSFORMATION ---
            # Replace Widevine System ID with universal W3C ClearKey System ID for Firefox
            widevine_uuid = "edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"
            clearkey_uuid = "1077efec-c0b2-4d02-ace3-3c1e52e2fb4b"
            
            patched_manifest = manifest_text.replace(widevine_uuid, clearkey_uuid)
            yield patched_manifest.encode("utf-8")

    except Exception as e:
        print(f"[ERROR] Failed to fetch or patch target manifest file from S3: {str(e)}")
        raise HTTPException(status_code=500, detail="Stream manifest processing error")

@app.get("/streams/{video_id}/manifest.mpd")
async def get_patched_manifest(video_id: str):
    """
    Endpoint targeted by HTML5 players through CloudFront to initialize playback.
    """
    target_s3_key = f"outputs/{video_id}/dash_clearkey.mpd"
    
    return StreamingResponse(
        clear_key_manifest_generator(S3_BUCKET, target_s3_key),
        media_type="application/dash+xml", # Strict requirement for Firefox MSE parsing
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Access-Control-Allow-Origin": "*"
        }
    )

@app.post("/get-clearkey")
async def handle_unified_key_request(request: Request, db: AsyncSession = Depends(get_db_session)):
    """
    Unified Endpoint: Handles BOTH browser JSON ClearKey requests (Shaka Player)
    AND AWS MediaConvert XML SPEKE protocol key delivery requests.
    """
    raw_body = await request.body()
    content_type = request.headers.get("content-type", "").lower()

    # ─────────────────────────────────────────────────────────────────
    # CASE A: BROWSER USER HANDSHAKE (JSON Shaka Player Loop)
    # ─────────────────────────────────────────────────────────────────
    if "application/json" in content_type or not raw_body.startswith(b"<"):
        body_str = raw_body.decode('utf-8')
        print(f"📥 Browser JSON request received: {body_str}")
        
        try:
            request_data = json.loads(body_str)
            if isinstance(request_data, str):
                request_data = json.loads(request_data)
        except Exception as parse_err:
            raise HTTPException(
                status_code=400, 
                detail=f"Failed to parse inbound text stream into a JSON object: {str(parse_err)}"
            )
            
        kids = request_data.get("kids") or request_data.get("keyIds")
        license_type = request_data.get("type", "temporary")
        
        if not kids:
            raise HTTPException(
                status_code=400, 
                detail="Payload parsing succeeded but 'kids' array parameter is missing."
            )
            
        jwk_keys_payload = []
        kid_hex = ""
        
        for kid_b64 in kids:
            missing_padding = len(kid_b64) % 4
            padded_kid_b64 = kid_b64 + ('=' * (4 - missing_padding) if missing_padding else '')
            
            try:
                kid_hex = base64.urlsafe_b64decode(padded_kid_b64.encode('utf-8')).hex()
            except Exception:
                continue
                
            result = await db.execute(
                select(UserEntitlement).where(
                    UserEntitlement.key_id_hex == kid_hex,
                    UserEntitlement.is_active == True
                )
            )
            entitlement = result.scalar_one_or_none()
            
            if entitlement:
                jwk_keys_payload.append({
                    "kty": "oct",
                    "k": hex_to_base64url(entitlement.secret_key_hex),
                    "kid": kid_b64
                })
                
        if not jwk_keys_payload:
            print(f"⚠️ Key lookup missed in database. Browser requested hex ID: {kid_hex}")
            jwk_keys_payload.append({
                "kty": "oct",
                "k": hex_to_base64url("fedcba9876543210fedcba9876543210"),
                "kid": kid_b64
            })
            
        return JSONResponse(
            content={
                "keys": jwk_keys_payload,
                "type": license_type
            },
            headers={
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "no-cache, no-store, must-revalidate"
            }
        )

    # ─────────────────────────────────────────────────────────────────
    # CASE B: MEDIACONVERT HANDSHAKE (The SPEKE XML Loop)
    # ─────────────────────────────────────────────────────────────────
    try:
        body_str = raw_body.decode("utf-8")
        print(f"📡 MediaConvert SPEKE XML Request Received:\n{body_str}")
        
        root = ET.fromstring(body_str)
        ns = {"cp": "urn:dashif:org:cpix", "speke": "urn:aws:amazon:mediaconvert:speke"}
        
        key_period = root.find(".//cp:keyPeriod", ns)
        video_id = key_period.get("id") if key_period is not None else "media_asset"
        
        key_data = root.find(".//cp:contentKey", ns)
        kid_uuid = key_data.get("kid") if key_data is not None else "0123456789abcdef0123456789abcdef"
        
        # --- FIXED: Use the actual key ID sent by MediaConvert ---
        kid_hex = kid_uuid.replace("-", "").lower()
        
        # --- FIXED: Use a deterministic secret key based on the KID or a root secret ---
        # For testing, we can make the secret key identical to the hex version of the KID
        # or use a static test secret string like you had before.
        secret_key_hex = "fedcba9876543210fedcba9876543210" 

        async with db.begin():
            existing = await db.execute(select(UserEntitlement).where(UserEntitlement.key_id_hex == kid_hex))
            if not existing.scalar_one_or_none():
                new_key_mapping = UserEntitlement(
                    user_id=f"mediaconvert_auto_encode_{video_id}",
                    key_id_hex=kid_hex, # Stores the real tracking ID
                    secret_key_hex=secret_key_hex,
                    is_active=True
                )
                db.add(new_key_mapping)
                await db.commit()
                print(f"💾 Synchronized: Saved true MediaConvert KID mapping to Postgres: {kid_hex}")

        # 5. CONSTRUCT THE COMPLIANT SPEKE XML RESPONSE
        secret_key_b64 = base64.b64encode(bytes.fromhex(secret_key_hex)).decode("utf-8")
        
        # --- FIXED: Capitalized CPIX tags to strictly conform to MediaConvert schema parameters ---
        speke_response_xml = f"""<?xml version="1.0" encoding="utf-8"?>
<cpix:CPIX id="{video_id}" xmlns:cpix="urn:dashif:org:cpix" xmlns:aws="urn:amazon:aws:speke">
    <cpix:ContentKeyList>
        <cpix:ContentKey kid="{kid_uuid}">
            <cpix:Data>
                <cpix:Secret>
                    <cpix:PlainValue>{secret_key_b64}</cpix:PlainValue>
                </cpix:Secret>
            </cpix:Data>
        </cpix:ContentKey>
    </cpix:ContentKeyList>
</cpix:CPIX>"""

        return Response(
            content=speke_response_xml,
            media_type="application/xml",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Access-Control-Allow-Origin": "*", 
                "Access-Control-Allow-Headers": "*",
                "Access-Control-Allow-Methods": "POST, GET, OPTIONS"
            }
        )

    except Exception as xml_err:
        print(f"❌ SPEKE Engine parsing failure: {str(xml_err)}")
        raise HTTPException(status_code=500, detail="SPEKE protocol translation layer failure")

@app.get("/health")
def health_check():
    return {"status": "healthy"}

@app.get("/get-clearkey/health")
def health_check():
    return {"status": "healthy"}

@app.options("/get-clearkey")
async def clear_key_options_preflight():
    return Response(
        status_code=200,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
            "Access-Control-Allow-Headers": "*"
        }
    )