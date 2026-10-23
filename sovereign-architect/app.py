import os
import logging
import json
from datetime import datetime
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from google.cloud import storage
from dotenv import load_dotenv
import vertexai
from vertexai.generative_models import GenerativeModel, Tool, grounding
from google import genai
import urllib.parse
import hashlib
import requests
import base64
import mcp_tools
from google.adk.agents import LlmAgent
import dev_card_tools
import watermark
import fallback_queue
from retry_utils import retry_with_backoff
import launchsense_agents
import launchsense_pdf as ls_pdf

# Load environment variables from .env file
load_dotenv()

# Configure basic logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("SovereignArchitect")

app = Flask(__name__)
CORS(app)

# Ensure the storage client is initialized with the Project Number
PROJECT_NUMBER = os.environ.get("PROJECT_NUMBER")
if PROJECT_NUMBER:
    storage_client = storage.Client(project=PROJECT_NUMBER)
else:
    logger.warning("PROJECT_NUMBER is not set. Storage client may fail.")
    storage_client = storage.Client()

BUCKET_NAME = os.environ.get("CLOUD_STORAGE_BUCKET", "nsk-asset-dna-493269616772")

# Initialize Vertex AI
PROJECT_ID = os.environ.get("PROJECT_ID", "antigravity-camp-nsk")
vertexai.init(project=PROJECT_ID, location="us-east4")

# Load system instructions
try:
    with open('.agent/rules/executive_architect.md', 'r') as f:
        SYSTEM_INSTRUCTION = f.read()
except FileNotFoundError:
    logger.warning("System instruction file not found. Falling back to default.")
    SYSTEM_INSTRUCTION = "You are the Senior Continental Strategist & Legal Architect."

# Initialize the Gemini model (using new google-genai SDK)
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
client = None

try:
    client = genai.Client(vertexai=True, project=PROJECT_ID, location="us-east4")
    logger.info("Gemini GenAI client initialized successfully with Vertex AI.")
except Exception as e:
    logger.error(f"Failed to initialize Gemini client: {e}")

# Initialize ADK Location Intelligence Agent
adk_agent = None
try:
    logger.info("Initializing ADK MCP Toolsets...")
    maps_toolset = mcp_tools.get_maps_mcp_toolset()
    bigquery_toolset = mcp_tools.get_bigquery_mcp_toolset()
    
    adk_agent = LlmAgent(
        model='gemini-3.1-pro-preview',
        name='Sovereign_Asset_Auditor',
        instruction=f"You are the Senior Continental Strategist & Legal Architect.\n"
                    f"You help conduct Feasibility Audits for Sovereign Green Energy Hubs in South Africa.\n"
                    f"1. Use the BigQuery toolset to access real-world infrastructure and constraint data.\n"
                    f"- Table `antigravity-camp-nsk.energy_infrastructure.sa_grid` contains electrical grid lines.\n"
                    f"- Table `antigravity-camp-nsk.energy_infrastructure.redz_zones` contains official Renewable Energy Development Zones.\n"
                    f"- Table `antigravity-camp-nsk.energy_infrastructure.protected_areas` contains National Parks and Nature Reserves.\n"
                    f"Columns for protected_areas: [name, type, radius_km, geometry (POINT string)].\n"
                    f"To query geography, use: ST_GEOGFROMTEXT(geometry) for redz_zones/protected_areas and ST_GEOGFROMGEOJSON(geometry) for sa_grid.\n"
                    f"Run all query jobs from project id: {PROJECT_ID}.\n"
                    f"2. Use the Maps toolset to analyze proximity to roads and towns.\n"
                    f"3. ALWAYS include a 'Legal & Environmental' section in your reports. If a plot is near or inside a protected area, flag it as high-risk.\n"
                    f"Always provide data-backed reports for land development viability.",
        tools=[maps_toolset, bigquery_toolset]
    )
    logger.info("ADK Location Intelligence Agent initialized successfully.")
except Exception as e:
    logger.error(f"Failed to initialize ADK Agent: {e}")

# Initialize GitHub Dev Card Agent
github_card_agent = None
try:
    github_card_agent = LlmAgent(
        model='gemini-2.5-flash',
        name='github_card_agent',
        instruction="You are a GitHub profile analyst and dev card generator. When a user gives you a GitHub username, you ALWAYS follow this exact sequence: first call scrape_github, then analyze_profile with the result, then generate_card_html with all three inputs, then save_card. Never skip steps. Be enthusiastic about developers' work. If the profile is private or doesn't exist, say so clearly.",
        tools=[
            dev_card_tools.scrape_github,
            dev_card_tools.analyze_profile,
            dev_card_tools.generate_card_html,
            dev_card_tools.save_card
        ]
    )
    logger.info("GitHub Dev Card Agent initialized successfully.")
except Exception as e:
    logger.error(f"Failed to initialize GitHub Dev Card Agent: {e}")

# Setup Google Search Grounding Tool
@retry_with_backoff(retries=3, backoff_in_seconds=2)
def generate_with_search(prompt: str):
    return client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=genai.types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            tools=[genai.types.Tool(google_search=genai.types.GoogleSearchRetrieval())]
        )
    )

# --- GENERATION HELPERS ---

@retry_with_backoff(retries=3, backoff_in_seconds=2)
def generate_basic(prompt: str):
    return client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=genai.types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION
        )
    )

@app.after_request
def add_header(response):
    """Allow the app to be embedded in the NSK Hub Empire."""
    response.headers['X-Frame-Options'] = 'ALLOWALL'
    response.headers['Content-Security-Policy'] = "frame-ancestors 'self' https://*.vercel.app https://*.nskdevpreneur-hub.com https://*.nsk-forge.com http://localhost:3000 http://127.0.0.1:3000;"
    return response

@app.route('/', methods=['GET'])
def index():
    """Serve the Command Center UI."""
    paypal_client_id = os.environ.get('PAYPAL_CLIENT_ID', 'test')
    
    # Capture paid status parameter passed from parent React wrapper
    paid_status = request.args.get('paid', 'false').lower() == 'true'
    
    # Resolve parent origin from query args or Referer header to redirect back correctly after PayFast payment
    parent_origin = request.args.get('parent_origin')
    if not parent_origin:
        referer = request.headers.get('Referer')
        if referer and 'run.app' not in referer:
            try:
                parsed_ref = urllib.parse.urlparse(referer)
                parent_origin = f"{parsed_ref.scheme}://{parsed_ref.netloc}"
            except Exception:
                pass
    if not parent_origin:
        parent_origin = "https://devpreneur-hub.vercel.app"
        
    return render_template('index.html', paypal_client_id=paypal_client_id, parent_origin=parent_origin, is_paid=paid_status)

@app.route('/success', methods=['GET'])
def payfast_success():
    """Backup success route that redirects back to the main site."""
    return render_template('paypal_success.html')

@app.route('/cancel', methods=['GET'])
def payfast_cancel():
    """Backup cancel route that redirects back to the main site."""
    return render_template('paypal_cancel.html')

@app.route('/checkout', methods=['POST'])
def process_checkout():
    """Generates a secure PayFast payment payload and signature."""
    data = request.get_json()
    if not data or 'amount' not in data:
        return jsonify({"status": "error", "message": "Invalid payment payload"}), 400
        
    amount = data.get('amount')
    name = data.get('name', 'Client')
    email = data.get('email', 'client@example.com')
    item_name = data.get('item_name', 'Architect Retainer Fee')
    
    parent_origin = data.get('parent_origin', '').strip()
    if not parent_origin:
        parent_origin = request.url_root.rstrip('/')
        return_path = f"{parent_origin}/success"
        cancel_path = f"{parent_origin}/cancel"
    else:
        return_path = f"{parent_origin}/foundry?payment=success"
        cancel_path = f"{parent_origin}/foundry?payment=cancel"
    
    merchant_id = os.environ.get('PAYFAST_MERCHANT_ID', '')
    merchant_key = os.environ.get('PAYFAST_MERCHANT_KEY', '')
    passphrase = os.environ.get('PAYFAST_PASSPHRASE', '')
    
    if not merchant_id or not merchant_key:
        return jsonify({"status": "error", "message": "PayFast API Keys not configured on server."}), 500
        
    # Construct PayFast payload
    # Must be in order according to PayFast docs for signature generation
    payload = {
        'merchant_id': merchant_id,
        'merchant_key': merchant_key,
        'return_url': return_path,
        'cancel_url': cancel_path,
        'notify_url': request.url_root + 'webhook/payfast/itn',
        'name_first': name,
        'email_address': email,
        'm_payment_id': 'RET-' + os.urandom(4).hex().upper(),
        'amount': str(amount),
        'item_name': item_name
    }
    
    # Generate Signature
    # PayFast expects urlencoded data (spaces encoded as '+', which urllib.parse.urlencode handles natively)
    pf_string = urllib.parse.urlencode(payload)
    if passphrase:
        pf_string += f"&passphrase={urllib.parse.quote_plus(passphrase.strip())}"
        
    signature = hashlib.md5(pf_string.encode()).hexdigest()
    payload['signature'] = signature
    
    # Return payload to frontend so it can create a form and submit
    return jsonify({
        "status": "success",
        "payfast_url": "https://sandbox.payfast.co.za/eng/process", # Change to www.payfast.co.za for production
        "payload": payload
    }), 200

@app.route('/webhook/payfast/itn', methods=['POST'])
def payfast_itn():
    """Listens for Instant Transaction Notifications from PayFast."""
    pf_data = request.form.to_dict()
    
    # Validate Signature
    signature = pf_data.pop('signature', None)
    passphrase = os.environ.get('PAYFAST_PASSPHRASE', '')
    
    pf_string = urllib.parse.urlencode(pf_data)
    if passphrase:
        pf_string += f"&passphrase={urllib.parse.quote_plus(passphrase.strip())}"
        
    expected_signature = hashlib.md5(pf_string.encode()).hexdigest()
    
    if signature != expected_signature:
        logger.error("PayFast Webhook: Invalid Signature")
        return "Invalid Signature", 400
        
    # Check payment status
    payment_status = pf_data.get('payment_status')
    if payment_status == 'COMPLETE':
        logger.info(f"Retainer Paid via PayFast! Order ID: {pf_data.get('m_payment_id')}")
        # Here we would officially unlock the user in our database
        
    return "OK", 200

# --- PAYPAL INTEGRATION ---
PAYPAL_TRANSACTION_LOG = 'paypal_transactions.jsonl'
PAYPAL_MODE = os.environ.get('PAYPAL_MODE', 'sandbox')
PAYPAL_WEBHOOK_ID = os.environ.get('PAYPAL_WEBHOOK_ID', '')

def get_paypal_base_url():
    """Get the appropriate PayPal base URL based on mode."""
    return "https://api-m.paypal.com" if PAYPAL_MODE == 'production' else "https://api-m.sandbox.paypal.com"

def get_paypal_access_token():
    """Obtain OAuth2 access token from PayPal."""
    client_id = os.environ.get('PAYPAL_CLIENT_ID')
    secret = os.environ.get('PAYPAL_SECRET')
    if not client_id or not secret:
        return None
        
    auth = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    base_url = get_paypal_base_url()
    response = requests.post(
        f"{base_url}/v1/oauth2/token",
        headers={"Authorization": f"Basic {auth}"},
        data={"grant_type": "client_credentials"}
    )
    if response.status_code == 200:
        return response.json().get('access_token')
    logger.error(f"PayPal token error: {response.status_code} - {response.text}")
    return None

def log_paypal_transaction(transaction_data):
    """Log PayPal transaction to JSONL file for persistence."""
    try:
        transaction_data['timestamp'] = datetime.utcnow().isoformat()
        with open(PAYPAL_TRANSACTION_LOG, 'a') as f:
            f.write(json.dumps(transaction_data) + '\n')
        logger.info(f"Transaction logged: {transaction_data.get('order_id')}")
    except Exception as e:
        logger.error(f"Failed to log transaction: {str(e)}")

def get_paypal_transaction(order_id):
    """Retrieve a transaction from the log by order ID."""
    try:
        with open(PAYPAL_TRANSACTION_LOG, 'r') as f:
            for line in f:
                record = json.loads(line)
                if record.get('order_id') == order_id:
                    return record
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.error(f"Failed to retrieve transaction: {str(e)}")
    return None

def verify_paypal_webhook_signature(transmission_id, timestamp, webhook_id, event_body, cert_url):
    """Verify PayPal webhook signature using certificate validation."""
    try:
        # For webhook verification, PayPal uses HMAC-SHA256 with cert validation
        # We'll fetch the cert from PayPal and verify the signature
        access_token = get_paypal_access_token()
        if not access_token:
            logger.error("Cannot verify webhook: no access token")
            return False
        
        verification_payload = {
            "transmission_id": transmission_id,
            "transmission_time": timestamp,
            "cert_url": cert_url,
            "auth_algo": "SHA256withRSA",
            "transmission_sig": request.headers.get('PAYPAL-TRANSMISSION-SIG', ''),
            "webhook_id": webhook_id,
            "webhook_event": json.loads(event_body) if isinstance(event_body, str) else event_body
        }
        
        base_url = get_paypal_base_url()
        response = requests.post(
            f"{base_url}/v1/notifications/verify-webhook-signature",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {access_token}"
            },
            json=verification_payload
        )
        
        if response.status_code == 200:
            result = response.json()
            return result.get('verification_status') == 'SUCCESS'
        else:
            logger.error(f"Webhook verification failed: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        logger.error(f"Webhook signature verification error: {str(e)}")
        return False

@app.route('/paypal/create-order-dynamic', methods=['POST'])
def paypal_create_order_dynamic():
    """Creates a PayPal order with dynamic amount/item."""
    data = request.get_json() or {}
    amount_usd = data.get('amount', '5000.00')
    item_description = data.get('description', 'Architect Retainer Fee (Global)')

    access_token = get_paypal_access_token()
    if not access_token:
        logger.info("PayPal API keys not configured. Enabling high-fidelity Simulation Mode.")
        mock_order_id = "MOCK-PAYPAL-" + os.urandom(4).hex().upper()
        return jsonify({
            "status": "simulation",
            "id": mock_order_id,
            "message": "Simulation Mode: Order generated locally."
        }), 200

    order_payload = {
        "intent": "CAPTURE",
        "purchase_units": [{
            "amount": {
                "currency_code": "USD",
                "value": str(amount_usd)
            },
            "description": item_description
        }],
        "application_context": {
            "return_url": request.url_root + "paypal-success",
            "cancel_url": request.url_root + "paypal-cancel",
            "user_action": "PAY_NOW"
        }
    }

    base_url = get_paypal_base_url()
    response = requests.post(
        f"{base_url}/v2/checkout/orders",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}"
        },
        json=order_payload
    )
    
    if response.status_code in [200, 201]:
        res_data = response.json()
        log_paypal_transaction({
            'order_id': res_data.get('id'),
            'status': 'CREATED',
            'amount': '5000.00',
            'currency': 'USD',
            'source': 'create-order'
        })
        return jsonify(res_data), response.status_code
    
    logger.error(f"PayPal order creation failed: {response.status_code} - {response.text}")
    return jsonify({"error": response.json()}), response.status_code

@app.route('/paypal/capture-order', methods=['POST'])
def paypal_capture_order():
    """Captures payment after customer approves on PayPal."""
    data = request.get_json()
    order_id = data.get('orderID')
    
    if not order_id:
        return jsonify({"error": "Missing orderID"}), 400
    
    if order_id.startswith("MOCK-PAYPAL-"):
        logger.info(f"Simulated PayPal capture: {order_id}")
        capture_id = "MOCK-CAP-" + os.urandom(4).hex().upper()
        log_paypal_transaction({
            'order_id': order_id,
            'capture_id': capture_id,
            'status': 'COMPLETED',
            'amount': '5000.00',
            'currency': 'USD',
            'source': 'capture-order'
        })
        return jsonify({
            "status": "COMPLETED",
            "id": order_id,
            "purchase_units": [{
                "payments": {
                    "captures": [{
                        "id": capture_id,
                        "status": "COMPLETED",
                        "amount": {"currency_code": "USD", "value": "5000.00"}
                    }]
                }
            }]
        }), 200
    
    # Check if already captured (idempotency)
    existing_txn = get_paypal_transaction(order_id)
    if existing_txn and existing_txn.get('status') == 'COMPLETED':
        logger.info(f"Order already captured: {order_id}")
        return jsonify({
            "status": "COMPLETED",
            "id": order_id,
            "message": "Order was already captured"
        }), 200
    
    access_token = get_paypal_access_token()
    if not access_token:
        return jsonify({"error": "Failed to authenticate with PayPal"}), 500

    base_url = get_paypal_base_url()
    response = requests.post(
        f"{base_url}/v2/checkout/orders/{order_id}/capture",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}"
        }
    )
    
    res_data = response.json()
    if response.status_code in [200, 201]:
        capture_id = None
        if 'purchase_units' in res_data:
            for unit in res_data['purchase_units']:
                if 'payments' in unit and 'captures' in unit['payments']:
                    for capture in unit['payments']['captures']:
                        capture_id = capture.get('id')
                        break
        
        log_paypal_transaction({
            'order_id': order_id,
            'capture_id': capture_id,
            'status': 'COMPLETED',
            'amount': '5000.00',
            'currency': 'USD',
            'source': 'capture-order'
        })
        logger.info(f"PayPal payment captured! Order: {order_id}, Capture: {capture_id}")
    else:
        logger.error(f"PayPal capture failed: {response.status_code} - {response.text}")
        
    return jsonify(res_data), response.status_code

@app.route('/webhook/paypal', methods=['POST'])
def paypal_webhook():
    """Handle PayPal Instant Payment Notification (IPN) webhook."""
    try:
        # Get webhook headers
        transmission_id = request.headers.get('PAYPAL-TRANSMISSION-ID')
        timestamp = request.headers.get('PAYPAL-TRANSMISSION-TIME')
        webhook_id = request.headers.get('PAYPAL-WEBHOOK-ID')
        cert_url = request.headers.get('PAYPAL-CERT-URL')
        
        # Get raw event body for signature verification
        event_body = request.get_data(as_text=True)
        event_data = json.loads(event_body)
        
        # Verify webhook signature
        if not verify_paypal_webhook_signature(transmission_id, timestamp, webhook_id, event_body, cert_url):
            logger.warning(f"Invalid PayPal webhook signature. ID: {transmission_id}")
            return jsonify({"status": "invalid_signature"}), 403
        
        # Process the event
        event_type = event_data.get('event_type')
        resource = event_data.get('resource', {})
        
        logger.info(f"PayPal Webhook: {event_type} - Order: {resource.get('id')}")
        
        # Handle different event types
        if event_type == 'CHECKOUT.ORDER.COMPLETED':
            order_id = resource.get('id')
            status = resource.get('status')
            
            log_paypal_transaction({
                'order_id': order_id,
                'status': status,
                'event_type': event_type,
                'amount': '5000.00',
                'currency': 'USD',
                'source': 'webhook'
            })
            logger.info(f"Order completed via webhook: {order_id}")
            
        elif event_type == 'PAYMENT.CAPTURE.COMPLETED':
            order_id = resource.get('supplementary_data', {}).get('related_ids', {}).get('order_id')
            capture_id = resource.get('id')
            status = resource.get('status')
            
            log_paypal_transaction({
                'order_id': order_id,
                'capture_id': capture_id,
                'status': status,
                'event_type': event_type,
                'amount': resource.get('amount', {}).get('value'),
                'currency': resource.get('amount', {}).get('currency_code'),
                'source': 'webhook'
            })
            logger.info(f"Payment captured via webhook: {order_id} -> {capture_id}")
            
        elif event_type == 'PAYMENT.CAPTURE.REFUNDED':
            order_id = resource.get('supplementary_data', {}).get('related_ids', {}).get('order_id')
            refund_id = resource.get('id')
            
            log_paypal_transaction({
                'order_id': order_id,
                'refund_id': refund_id,
                'event_type': event_type,
                'amount': resource.get('amount', {}).get('value'),
                'currency': resource.get('amount', {}).get('currency_code'),
                'source': 'webhook',
                'status': 'REFUNDED'
            })
            logger.warning(f"Payment refunded: {order_id} -> {refund_id}")
        
        # PayPal requires immediate 200 OK response
        return jsonify({"status": "received"}), 200
        
    except json.JSONDecodeError:
        logger.error("Invalid JSON in PayPal webhook")
        return jsonify({"error": "Invalid JSON"}), 400
    except Exception as e:
        logger.error(f"PayPal webhook error: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/paypal-success', methods=['GET'])
def paypal_success():
    """Redirect page after successful PayPal payment."""
    return render_template('paypal_success.html')

@app.route('/paypal-cancel', methods=['GET'])
def paypal_cancel():
    """Redirect page if customer cancels PayPal payment."""
    return render_template('paypal_cancel.html')

@app.route('/ingest-asset', methods=['POST'])
def ingest_asset():
    """For ingesting legal PDFs and land imagery into GCS."""
    if 'file' not in request.files:
        return jsonify({"status": "error", "message": "No file part in the request"}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({"status": "error", "message": "No selected file"}), 400

    try:
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(file.filename)
        blob.upload_from_file(file)
        
        logger.info(f"Asset {file.filename} ingested successfully into {BUCKET_NAME}")
        
        gs_uri = f"gs://{BUCKET_NAME}/{file.filename}"
        extracted_dna = ""
        
        # Analyze the document to extract Asset DNA
        if client:
            try:
                prompt = """
                You are the Sovereign Asset Architect. 
                Please analyze this uploaded document and extract its "Asset DNA".
                Provide a clear, structured summary including:
                1. Asset/Document Type
                2. Key Parties / Entities Involved
                3. Core Subject Matter or Property Location
                4. Key Terms & Obligations
                5. Potential Legal or Feasibility Risks
                
                Keep the output concise, professional, and formatted in clear text (avoid markdown symbols like asterisks or hashes if possible, just use standard formatting).
                """
                
                analysis_response = client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=[
                        genai.types.Part.from_uri(
                            file_uri=gs_uri,
                            mime_type=file.mimetype,
                        ),
                        prompt
                    ],
                    config=genai.types.GenerateContentConfig(
                        system_instruction=SYSTEM_INSTRUCTION
                    )
                )
                extracted_dna = analysis_response.text
                
                # Apply Invisible Watermark
                payload = f"NSK-SECURED|AssetDNA|{datetime.utcnow().isoformat()}"
                extracted_dna = watermark.encode_watermark(extracted_dna, payload)
            except Exception as ai_e:
                logger.error(f"Asset DNA extraction failed: {str(ai_e)}")
                fallback_queue.log_failed_request('/ingest-asset', {"asset_filename": file.filename}, str(ai_e))
                extracted_dna = "Asset securely stored. AI extraction queued due to high demand. Processing will complete within 12 hours."
        else:
            extracted_dna = "Gemini client not initialized. Cannot extract Asset DNA."
        
        return jsonify({
            "status": "success",
            "message": f"Asset '{file.filename}' ingested successfully.",
            "storage_uri": gs_uri,
            "asset_dna": extracted_dna
        }), 200
    except Exception as e:
        logger.error(f"Failed to ingest asset: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/ingest-regulation', methods=['POST'])
def ingest_regulation():
    """Ingests regulatory PDFs (e.g. Gazettes) and extracts vector-ready compliance rules."""
    if 'file' not in request.files:
        return jsonify({"status": "error", "message": "No file part in the request"}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({"status": "error", "message": "No selected file"}), 400

    try:
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(f"regulations/{file.filename}")
        blob.upload_from_file(file)
        
        logger.info(f"Regulatory document {file.filename} uploaded successfully into GCS")
        
        gs_uri = f"gs://{BUCKET_NAME}/regulations/{file.filename}"
        extracted_rules = {}

        if client:
            try:
                # Prompt to extract structured regulatory compliance rules
                prompt = """
                You are the Regulatory Compliance AI Architect.
                Please analyze this legislative document/government gazette and output a structured JSON schema.
                
                The JSON MUST contain:
                {
                  "regulation_title": "Official Title of the Act/Gazette",
                  "jurisdiction": "e.g., South Africa (National / Provincial)",
                  "effective_date": "YYYY-MM-DD",
                  "compliance_rules": [
                    {
                      "clause_id": "e.g., Section 12(1)(a)",
                      "rule_name": "Name of the rule/constraint",
                      "criteria": "Detailed compliance criteria or legal rule",
                      "impact_level": "CRITICAL / MEDIUM / LOW"
                    }
                  ],
                  "verification_metric": "How an automated software system can verify compliance to this rule"
                }
                
                Return ONLY raw JSON. Do not wrap it in markdown code blocks or add any comments.
                """
                
                analysis_response = client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=[
                        genai.types.Part.from_uri(
                            file_uri=gs_uri,
                            mime_type=file.mimetype,
                        ),
                        prompt
                      ]
                )
                
                response_text = analysis_response.text.strip()
                # Clean any accidental markdown wrappers if the model generated them
                if response_text.startswith("```json"):
                    response_text = response_text.replace("```json", "", 1)
                if response_text.endswith("```"):
                    response_text = response_text.rsplit("```", 1)[0]
                
                extracted_rules = json.loads(response_text.strip())
            except Exception as ai_e:
                logger.error(f"Regulatory rule extraction failed: {str(ai_e)}")
                extracted_rules = {
                    "error": "AI Extraction failed",
                    "details": str(ai_e)
                }
        else:
            extracted_rules = {"error": "Gemini client not initialized."}
            
        return jsonify({
            "status": "success",
            "message": f"Regulatory document '{file.filename}' ingested and parsed successfully.",
            "storage_uri": gs_uri,
            "rules": extracted_rules
        }), 200
        
    except Exception as e:
        logger.error(f"Failed to ingest regulation: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/query-trends', methods=['POST'])
def query_trends():
    """Aggregates a list of developer queries and clusters them into top developer demand trends."""
    data = request.get_json()
    if not data or 'queries' not in data:
        return jsonify({"status": "error", "message": "Missing 'queries' in JSON payload."}), 400
        
    queries = data['queries']
    if not queries:
        return jsonify({"status": "success", "trends": []}), 200
        
    if not client:
        return jsonify({"status": "error", "message": "Gemini client not initialized."}), 500
        
    try:
        # Prompt to analyze and cluster queries
        prompt = f"""
        You are the Platform Product Architect.
        Please analyze this list of developer search queries and support questions:
        {json.dumps(queries)}
        
        Cluster these queries into the top 3-5 trending feature requests or API needs.
        Output a structured JSON array containing:
        [
          {{
            "topic": "Name of the requested feature/API (e.g. FICA Verification, SARS Tax)",
            "request_count": 12, // estimated relative volume based on the list
            "description": "Short explanation of what developers are trying to achieve",
            "suggested_api_endpoint": "e.g. GET /api/v1/forge/fica"
          }}
        ]
        
        Return ONLY raw JSON. Do not wrap it in markdown code blocks or add comments.
        """
        
        analysis_response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[prompt]
        )
        
        response_text = analysis_response.text.strip()
        if response_text.startswith("```json"):
            response_text = response_text.replace("```json", "", 1)
        if response_text.endswith("```"):
            response_text = response_text.rsplit("```", 1)[0]
            
        trends = json.loads(response_text.strip())
        return jsonify({
            "status": "success",
            "trends": trends
        }), 200
        
    except Exception as e:
        logger.error(f"Failed to aggregate query trends: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/forge-contract', methods=['POST'])
def forge_contract():
    """To trigger the generation of 'Executive Contracts'."""
    data = request.get_json()
    if not data or 'context' not in data:
        return jsonify({"status": "error", "message": "Missing 'context' in JSON payload."}), 400
        
    context = data['context']
    prompt = f"Please forge an Executive Contract based on the following context:\n\n{context}"
    
    if not client:
        return jsonify({"status": "error", "message": "Gemini AI client is not initialized. Check server logs."}), 503
        
    try:
        response = generate_basic(prompt)
        contract_text = response.text
        
        # Apply Invisible Watermark
        payload = f"NSK-SECURED|Contract|{datetime.utcnow().isoformat()}"
        contract_text = watermark.encode_watermark(contract_text, payload)
        
        return jsonify({
            "status": "success", 
            "message": "Executive contract forged successfully",
            "contract": contract_text
        }), 200
    except Exception as e:
        logger.error(f"Failed to forge contract: {str(e)}")
        fallback_queue.log_failed_request('/forge-contract', data, str(e))
        return jsonify({
            "status": "pending", 
            "message": "Due to unprecedented demand on the Sovereign Architect, your request has been securely queued. Your completed asset will be delivered via secure email within 12 hours. Your transaction is safe."
        }), 202

@app.route('/feasibility-audit', methods=['POST'])
def feasibility_audit():
    """To run data-backed feasibility audits using ADK and MCP."""
    data = request.get_json()
    if not data or 'query' not in data:
        return jsonify({"status": "error", "message": "Missing 'query' in JSON payload."}), 400
        
    query = data['query']
    prompt = f"Please conduct a feasibility audit for the following inquiry using your MCP tools:\n\n{query}"
    
    if not adk_agent:
        # Fallback to standard GenAI client if ADK is down
        if not client:
            return jsonify({"status": "error", "message": "No AI clients initialized."}), 503
        try:
            response = generate_with_search(prompt)
            report_text = response.text
            
            # Apply Invisible Watermark
            payload = f"NSK-SECURED|Audit|{datetime.utcnow().isoformat()}"
            report_text = watermark.encode_watermark(report_text, payload)
            
            return jsonify({
                "status": "success", 
                "message": "Audit completed via basic Search Grounding (ADK offline)",
                "report": report_text
            }), 200
        except Exception as e:
            fallback_queue.log_failed_request('/feasibility-audit', data, str(e))
            return jsonify({
                "status": "pending", 
                "message": "Due to unprecedented demand on the Sovereign Architect, your request has been securely queued. Your completed asset will be delivered via secure email within 12 hours. Your transaction is safe."
            }), 202

    try:
        # Bypass InMemoryRunner due to SQLite thread-locking issues in Flask
        response = generate_with_search(prompt)
        report_text = response.text
        
        # Apply Invisible Watermark
        payload = f"NSK-SECURED|Audit|{datetime.utcnow().isoformat()}"
        report_text = watermark.encode_watermark(report_text, payload)
        
        return jsonify({
            "status": "success", 
            "message": "Feasibility audit completed successfully using basic Search Grounding",
            "report": report_text
        }), 200
    except Exception as e:
        logger.error(f"Failed to run ADK feasibility audit: {str(e)}")
        fallback_queue.log_failed_request('/feasibility-audit', data, str(e))
        return jsonify({
            "status": "pending", 
            "message": "Due to unprecedented demand on the Sovereign Architect, your request has been securely queued. Your completed asset will be delivered via secure email within 12 hours. Your transaction is safe."
        }), 202

@app.route('/generate-dev-card', methods=['POST'])
def generate_dev_card():
    """Generates a Developer Persona Card using the Dev Card Agent."""
    data = request.get_json()
    if not data or 'username' not in data:
        return jsonify({"status": "error", "message": "Missing 'username' in JSON payload."}), 400
        
    username = data['username']
    prompt = f"Generate a dev card for {username}"
    
    if not github_card_agent:
        return jsonify({"status": "error", "message": "GitHub Dev Card Agent is not initialized."}), 503

    try:
        # Since the Agent's system instructions demand a strict deterministic sequence 
        # ("ALWAYS follow this exact sequence: scrape -> analyze -> generate -> save"),
        # and InMemoryRunner encounters SQLite thread-locking issues in Flask,
        # we bypass the runner and execute the exact agentic loop deterministically here:
        
        # 1. Scrape GitHub
        github_data = dev_card_tools.scrape_github(username)
        if "error" in github_data:
            return jsonify({"status": "error", "message": github_data["error"]}), 200
            
        # 2. Analyze Profile (Calls Gemini 2.5 Flash)
        analysis = dev_card_tools.analyze_profile(github_data)
        
        # 3. Generate HTML
        card_html = dev_card_tools.generate_card_html(username, github_data, analysis)
        
        # 4. Save Card
        card_url = dev_card_tools.save_card(username, card_html)
        
        if card_url.startswith("Error"):
            return jsonify({"status": "error", "message": card_url}), 500
            
        return jsonify({
            "status": "success",
            "message": f"Dev card generated successfully for {username}.",
            "card_url": card_url,
            "html": card_html
        }), 200
            
    except Exception as e:
        logger.error(f"Failed to generate dev card: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

# =====================================================================
# Self-Updating Agents Endpoints
# =====================================================================

@app.route('/api/register-agent', methods=['POST'])
@retry_with_backoff(retries=3, backoff_in_seconds=2)
def register_self_updating_agent():
    data = request.json
    username = data.get('username')
    token = data.get('token')
    
    if not username or not token:
        return jsonify({"status": "error", "message": "Username and GitHub PAT are required."}), 400
        
    success = dev_card_tools.register_agent(username, token)
    if success:
        logger.info(f"Successfully registered agent for {username}")
        return jsonify({"status": "success", "message": f"Autonomous Agent deployed for @{username}. Orbiting every 24h."})
    else:
        return jsonify({"status": "error", "message": "Failed to vault agent credentials."}), 500

@app.route('/api/cron/trigger-agents', methods=['POST', 'GET'])
def trigger_agents_cron():
    """
    Triggered by Google Cloud Scheduler every 24 hours.
    Loops through registered agents, generates updated cards, and pushes them to GitHub.
    """
    # In production, secure this endpoint with a header or token from Cloud Scheduler
    agents = dev_card_tools.get_registered_agents()
    if not agents:
        return jsonify({"status": "success", "message": "No active agents in registry."}), 200
        
    results = {}
    for username, config in agents.items():
        if not config.get('active', False):
            continue
            
        token = config.get('token')
        logger.info(f"Triggering auto-update for {username}...")
        
        # 1. Scrape latest stats
        github_data = dev_card_tools.scrape_github(username)
        if "error" in github_data:
            results[username] = "Failed to scrape"
            continue
            
        # 2. Analyze
        analysis = dev_card_tools.analyze_profile(github_data)
        
        # 3. Generate HTML
        card_html = dev_card_tools.generate_card_html(username, github_data, analysis)
        
        # 4. Push to GitHub
        success = dev_card_tools.update_github_readme(username, token, card_html)
        results[username] = "Success" if success else "Failed to push"
        
    return jsonify({"status": "success", "results": results}), 200

@app.route('/verify-watermark', methods=['POST'])
def verify_watermark():
    """Verifies and extracts the hidden invisible watermark from a document."""
    data = request.get_json()
    if not data or 'text' not in data:
        return jsonify({"status": "error", "message": "Missing 'text' in JSON payload."}), 400
        
    text = data['text']
    payload = watermark.decode_watermark(text)
    
    if payload:
        return jsonify({
            "status": "success",
            "watermark_found": True,
            "payload": payload,
            "message": "Authentic NSK Sovereign Asset Architect Signature Detected."
        }), 200
    else:
        return jsonify({
            "status": "success",
            "watermark_found": False,
            "message": "No valid NSK signature found in the provided text."
        }), 200

# =====================================================================
# LaunchSense Intelligence Endpoints
# =====================================================================

@app.route('/api/launchsense/validate', methods=['POST'])
def launchsense_validate():
    """Runs the LaunchSense Venture Validation."""
    data = request.get_json()
    if not data or 'product_brief' not in data:
        return jsonify({"status": "error", "message": "Missing product_brief."}), 400
        
    brief = data['product_brief']
    cost = float(data.get('cost_price', 150.0))
    margin = float(data.get('target_margin', 0.4))
    
    strategist = launchsense_agents.MasterStrategist()
    result = strategist.generate_full_strategy(brief, cost, margin)
    
    if "error" in result:
        return jsonify({"status": "error", "message": result["error"]}), 500
        
    result["brief"] = brief
    result["suggested_price"] = round(cost / (1 - margin), 2)
    return jsonify({"status": "success", "data": result}), 200

@app.route('/api/launchsense/sketch', methods=['POST'])
def launchsense_sketch():
    """Generates a base64 product sketch via Gemini."""
    data = request.get_json()
    if not data or 'product_brief' not in data:
        return jsonify({"status": "error", "message": "Missing product_brief."}), 400
        
    brief = data['product_brief']
    agent = launchsense_agents.ImageAgent()
    base64_img = agent.generate_sketch(brief)
    
    if base64_img:
        return jsonify({"status": "success", "image_base64": base64_img}), 200
    return jsonify({"status": "error", "message": "Image generation failed."}), 500

@app.route('/api/launchsense/pdf', methods=['POST'])
def launchsense_pdf():
    """Generates the Executive Strategy PDF. To be called after successful payment."""
    data = request.get_json()
    if not data or 'analysis_data' not in data:
        return jsonify({"status": "error", "message": "Missing analysis_data."}), 400
        
    analysis_data = data['analysis_data']
    try:
        if 'url' in analysis_data or 'overallScore' in analysis_data:
            pdf_bytes = ls_pdf.generate_auditor_pdf(analysis_data)
        else:
            pdf_bytes = ls_pdf.generate_professional_pdf(analysis_data)
        pdf_base64 = base64.b64encode(pdf_bytes).decode('utf-8')
        return jsonify({"status": "success", "pdf_base64": pdf_base64}), 200
    except Exception as e:
        logger.error(f"PDF generation failed: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/v1/generate-pdf', methods=['POST'])
def generate_generic_pdf():
    """Generates a premium typeset PDF from raw HTML using WeasyPrint."""
    data = request.get_json()
    if not data or 'html_content' not in data:
        return jsonify({"status": "error", "message": "Missing html_content."}), 400
        
    html_content = data['html_content']
    title = data.get('title', 'NSK Sovereign Document')
    
    try:
        # Load base template path
        template_path = os.path.join(os.path.dirname(__file__), 'templates', 'pdf_template.html')
        with open(template_path, 'r', encoding='utf-8') as f:
            template_html = f.read()
            
        # Inject variables
        full_html = template_html.replace('{{ title }}', title)
        full_html = full_html.replace('{{ content|safe }}', html_content)
        
        # Render using WeasyPrint
        from weasyprint import HTML
        pdf_bytes = HTML(string=full_html).write_pdf()
        pdf_base64 = base64.b64encode(pdf_bytes).decode('utf-8')
        
        return jsonify({"status": "success", "pdf_base64": pdf_base64}), 200
    except Exception as e:
        logger.error(f"Generic PDF generation failed: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    debug_mode = os.environ.get("FLASK_DEBUG", "False").lower() == "true"
    
    # If running locally on your laptop, use SSL. If running on Cloud Run, let Google handle it.
    if os.environ.get("K_SERVICE"):  # This variable only exists when live on Cloud Run
        app.run(debug=debug_mode, host='0.0.0.0', port=port)
    else:
        app.run(debug=debug_mode, host='0.0.0.0', port=port, ssl_context='adhoc')