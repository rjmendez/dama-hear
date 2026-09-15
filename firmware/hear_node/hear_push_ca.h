// Root CA for HEAR_PUSH_HOST's TLS certificate (Alert 3 remediation).
//
// HEAR_PUSH_HOST defaults to an AWS API Gateway custom domain, whose ACM certificate chains to
// one of Amazon Trust Services' own roots. This is "Amazon Root CA 1", published by Amazon at
// https://www.amazontrust.com/repository/AmazonRootCA1.pem specifically for embedding in device
// firmware that cannot fetch a CA bundle at runtime -- the same reason AWS's own IoT Device SDKs
// ship it verbatim. It is a public root certificate, not a secret: pinning it here lets
// WiFiClientSecure verify the push endpoint's certificate chain instead of calling
// client.setInsecure() and accepting anything that answers on the port.
//
// A build pointed at a different HEAR_PUSH_HOST (a different CA, or a private one) overrides this
// by defining HEAR_PUSH_CA_CERT in secrets.h BEFORE this header is reached -- see the #ifndef
// below and secrets.h.example.
#pragma once

#ifndef HEAR_PUSH_CA_CERT
#define HEAR_PUSH_CA_CERT \
"-----BEGIN CERTIFICATE-----\n" \
"MIIDQTCCAimgAwIBAgITBmyfz5m/jAo54vB4ikPmljZbyjANBgkqhkiG9w0BAQsF\n" \
"ADA5MQswCQYDVQQGEwJVUzEPMA0GA1UEChMGQW1hem9uMRkwFwYDVQQDExBBbWF6\n" \
"b24gUm9vdCBDQSAxMB4XDTE1MDUyNjAwMDAwMFoXDTM4MDExNzAwMDAwMFowOTEL\n" \
"MAkGA1UEBhMCVVMxDzANBgNVBAoTBkFtYXpvbjEZMBcGA1UEAxMQQW1hem9uIFJv\n" \
"b3QgQ0EgMTCCASIwDQYJKoZIhvcNAQEBBQADggEPADCCAQoCggEBALJ4gHHKeNXj\n" \
"ca9HgFB0fW7Y14h29Jlo91ghYPl0hAEvrAIthtOgQ3pOsqTQNroBvo3bSMgHFzZM\n" \
"9O6II8c+6zf1tRn4SWiw3te5djgdYZ6k/oI2peVKVuRF4fn9tBb6dNqcmzU5L/qw\n" \
"IFAGbHrQgLKm+a/sRxmPUDgH3KKHOVj4utWp+UhnMJbulHheb4mjUcAwhmahRWa6\n" \
"VOujw5H5SNz/0egwLX0tdHA114gk957EWW67c4cX8jJGKLhD+rcdqsq08p8kDi1L\n" \
"93FcXmn/6pUCyziKrlA4b9v7LWIbxcceVOF34GfID5yHI9Y/QCB/IIDEgEw+OyQm\n" \
"jgSubJrIqg0CAwEAAaNCMEAwDwYDVR0TAQH/BAUwAwEB/zAOBgNVHQ8BAf8EBAMC\n" \
"AYYwHQYDVR0OBBYEFIQYzIU07LwMlJQuCFmcx7IQTgoIMA0GCSqGSIb3DQEBCwUA\n" \
"A4IBAQCY8jdaQZChGsV2USggNiMOruYou6r4lK5IpDB/G/wkjUu0yKGX9rbxenDI\n" \
"U5PMCCjjmCXPI6T53iHTfIUJrU6adTrCC2qJeHZERxhlbI1Bjjt/msv0tadQ1wUs\n" \
"N+gDS63pYaACbvXy8MWy7Vu33PqUXHeeE6V/Uq2V8viTO96LXFvKWlJbYK8U90vv\n" \
"o/ufQJVtMVT8QtPHRh8jrdkPSHCa2XV4cdFyQzR1bldZwgJcJmApzyMZFo6IQ6XU\n" \
"5MsI+yMRQ+hDKXJioaldXgjUkK642M4UwtBV8ob2xJNDd2ZhwLnoQdeXeGADbkpy\n" \
"rqXRfboQnoZsG4q5WTP468SQvvG5\n" \
"-----END CERTIFICATE-----\n"
#endif

// Explicit escape hatch for a build that genuinely cannot pin a CA (e.g. a LAN receiver behind a
// self-signed cert with no distributable root). Off by default: HEAR_PUSH_TLS with no CA and no
// opt-in is a build-time error, not a silent client.setInsecure(). Set in secrets.h, never here.
#ifndef HEAR_PUSH_TLS_INSECURE
#define HEAR_PUSH_TLS_INSECURE 0
#endif
