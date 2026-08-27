"""
recommendations — a tiny service that plugs into mini Robot Shop.

It calls the catalogue service, so it appears in the Instana service map as
    recommendations -> catalogue -> mongodb
and, because requirements.txt pins outdated libraries, the image ships real
CVEs for the Trivy scan / IBM Concert to raise.

This is a workshop TEMPLATE: students build and push it through their pipeline.
"""
import os
import random

import requests
from flask import Flask, jsonify

app = Flask(__name__)

CATALOGUE_URL = os.environ.get("CATALOGUE_URL", "http://catalogue:8080")


@app.route("/health")
def health():
    return jsonify(app="OK")


@app.route("/recommendations")
def recommendations():
    # Downstream call -> this is the edge Instana traces.
    resp = requests.get(f"{CATALOGUE_URL}/products", timeout=5)
    products = resp.json()
    picks = random.sample(products, min(3, len(products)))
    return jsonify(recommended=[{"sku": p.get("sku"), "name": p.get("name")} for p in picks])


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
