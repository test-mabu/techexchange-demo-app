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
    
    @GetMapping("/")
    public ResponseEntity<Map<String, Object>> home() {
        long requestId = requestCounter.incrementAndGet();
        logger.info("Home endpoint accessed - Request #{}", requestId);
        
        Map<String, Object> response = new HashMap<>();
        response.put("service", "Vulnerable Echo Service");
        response.put("version", "1.0.0-VULNERABLE");
        response.put("vulnerability", "CVE-2021-44228");
        response.put("endpoints", new String[]{
            "GET / - Service info",
            "GET /health - Health check", 
            "POST /echo - Echo message (VULNERABLE)",
            "GET /echo/{message} - Echo via GET (VULNERABLE)",
            "GET /echo?message=text - Echo via Query Parameter (VULNERABLE)"
        });
        response.put("timestamp", LocalDateTime.now().format(DateTimeFormatter.ISO_LOCAL_DATE_TIME));
        response.put("request_id", requestId);
        
        return ResponseEntity.ok(response);
    }
    
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
