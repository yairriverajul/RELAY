import threading
from pathlib import Path
from typing import Optional

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from web3 import Web3

app = FastAPI()

# ================== CONFIGURACIÓN BLOCKCHAIN ==================

# RPC público de Sepolia (funciona sin API key).
# Si prefieres Alchemy, crea un app NUEVO en Ethereum Sepolia y pega aquí su URL.
RPC_URL = "https://ethereum-sepolia-rpc.publicnode.com"

CONTRACT_ADDRESS = Web3.to_checksum_address(
    "0xF598E4A59558463cE78b87F7B5b7c6Fe43326335"
)

# Private key de PRUEBA (solo Sepolia).
PRIVATE_KEY = "c0b927c04c3dd08d8951923103450a528d8d786cb153985bb6dde7c1d02c1ddf"

CHAIN_ID = 11155111  # Sepolia

# True: responde cuando la transacción ya fue minada (10-30 s).
# False: responde enseguida con el tx_hash (mejor si el ESP32 tiene timeout corto).
ESPERAR_MINADO = False

# Margen extra sobre el gas estimado (1.3 = 30 % más).
MARGEN_GAS = 1.3

# Archivo del panel web, ubicado junto a este archivo en el repositorio.
PANEL_HTML = Path(__file__).parent / "view.html"

ABI_JSON = [
    {
        "inputs": [
            {"internalType": "string", "name": "deviceId", "type": "string"},
            {"internalType": "int16", "name": "temperatureTimes10", "type": "int16"},
            {"internalType": "uint16", "name": "humidityTimes10", "type": "uint16"},
            {"internalType": "uint256", "name": "timestampMs", "type": "uint256"},
            {"internalType": "string", "name": "cid", "type": "string"},
        ],
        "name": "storeReading",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]

# ================== CONFIGURACIÓN PINATA ==================

# El JWT anterior está vencido. Pega aquí uno nuevo para guardar en IPFS.
# Si queda vacío, las lecturas se guardan igual en la blockchain con cid vacío.
PINATA_JWT = ""

PINATA_URL = "https://api.pinata.cloud/pinning/pinJSONToIPFS"

# ================== INICIALIZACIÓN WEB3 ==================

w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 30}))

# Crear cuenta y contrato no necesita red, así que el servicio siempre arranca.
account = w3.eth.account.from_key(PRIVATE_KEY)
contract = w3.eth.contract(address=CONTRACT_ADDRESS, abi=ABI_JSON)
print("[INFO] Relayer usando cuenta:", account.address)

try:
    print("[INFO] Conectado a Sepolia:", w3.is_connected())
except Exception as e:
    print("[WARN] No se pudo verificar la conexión al arrancar:", e)

# Evita que dos lecturas simultáneas usen el mismo nonce.
tx_lock = threading.Lock()


class Lectura(BaseModel):
    device_id: str = "unknown-device"
    temperature: float
    humidity: float
    timestamp_ms: int


# GET para el navegador y HEAD para el chequeo de salud de Render.
@app.api_route("/", methods=["GET", "HEAD"])
def root():
    return {"status": "ok", "message": "Relayer funcionando"}


@app.get("/view", response_class=HTMLResponse)
def ver_panel():
    """Muestra el panel web (view.html) que lee las lecturas desde Sepolia."""
    if not PANEL_HTML.exists():
        raise HTTPException(
            status_code=404,
            detail="No se encontró view.html en la raíz del repositorio",
        )
    return HTMLResponse(PANEL_HTML.read_text(encoding="utf-8"))


def subir_a_pinata(payload: dict) -> Optional[str]:
    """Sube un JSON a Pinata y devuelve el CID. Si falla, devuelve None."""
    if not PINATA_JWT:
        print("[WARN] PINATA_JWT vacío, no se subirá a IPFS.")
        return None

    headers = {
        "Authorization": f"Bearer {PINATA_JWT}",
        "Content-Type": "application/json",
    }
    try:
        r = requests.post(PINATA_URL, json=payload, headers=headers, timeout=30)
        r.raise_for_status()
        cid = r.json().get("IpfsHash")
        print("[INFO] Subido a Pinata, CID:", cid)
        return cid
    except Exception as e:
        print("[ERROR] Error subiendo a Pinata:", e)
        return None


# Endpoint síncrono: FastAPI lo ejecuta en un hilo y no bloquea al resto.
@app.post("/api/lecturas")
def recibir_lectura(lectura: Lectura):
    print("[DEBUG] Payload recibido:", lectura.model_dump())

    # 25.3 C -> 253, 70.1 % -> 701
    temp_times10 = int(round(lectura.temperature * 10))
    hum_times10 = int(round(lectura.humidity * 10))

    if not (-32768 <= temp_times10 <= 32767) or not (0 <= hum_times10 <= 65535):
        raise HTTPException(status_code=422, detail="Temperatura o humedad fuera de rango")

    # 1) Respaldo en IPFS (opcional)
    cid = subir_a_pinata(
        {
            "device_id": lectura.device_id,
            "temperature_c": lectura.temperature,
            "humidity_percent": lectura.humidity,
            "timestamp_ms": lectura.timestamp_ms,
        }
    ) or ""

    # 2) Enviar transacción a storeReading(...)
    try:
        with tx_lock:
            nonce = w3.eth.get_transaction_count(account.address, "pending")
            funcion = contract.functions.storeReading(
                lectura.device_id,
                temp_times10,
                hum_times10,
                lectura.timestamp_ms,
                cid,
            )
            # Estima el gas (falla aquí si el contrato rechazaría la lectura) y añade margen.
            gas_estimado = funcion.estimate_gas({"from": account.address})
            tx = funcion.build_transaction(
                {
                    "from": account.address,
                    "nonce": nonce,
                    "gas": int(gas_estimado * MARGEN_GAS),
                    "gasPrice": w3.eth.gas_price,
                    "chainId": CHAIN_ID,
                }
            )
            signed_tx = w3.eth.account.sign_transaction(tx, private_key=PRIVATE_KEY)
            tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
    except Exception as e:
        print("[ERROR] No se pudo enviar la transacción:", repr(e))
        raise HTTPException(status_code=502, detail=f"Error enviando transacción: {e}")

    tx_hash_hex = w3.to_hex(tx_hash)
    print("[INFO] Tx enviada:", tx_hash_hex)

    if not ESPERAR_MINADO:
        return {"status": "sent", "tx_hash": tx_hash_hex, "cid": cid}

    try:
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    except Exception as e:
        print("[ERROR] Tx enviada pero sin confirmar:", repr(e))
        raise HTTPException(
            status_code=504,
            detail=f"Transacción enviada ({tx_hash_hex}) pero aún sin confirmar",
        )

    print("[INFO] Tx minada en bloque:", receipt.blockNumber)

    if receipt.status != 1:
        raise HTTPException(
            status_code=500,
            detail=f"La transacción fue revertida por el contrato ({tx_hash_hex})",
        )

    return {
        "status": "ok",
        "tx_hash": tx_hash_hex,
        "block": receipt.blockNumber,
        "cid": cid,
    }


