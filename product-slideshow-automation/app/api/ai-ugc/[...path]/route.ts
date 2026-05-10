import { NextRequest, NextResponse } from "next/server";

type RouteContext = {
  params: Promise<{
    path?: string[];
  }>;
};

const DEFAULT_AI_UGC_BASE_URL = "http://localhost:3000";

function getAiUgcBaseUrl(): string {
  return (process.env.AI_UGC_BASE_URL || process.env.NEXT_PUBLIC_AI_UGC_BASE_URL || DEFAULT_AI_UGC_BASE_URL).replace(/\/$/, "");
}

async function proxyAiUgcRequest(request: NextRequest, context: RouteContext) {
  const params = await context.params;
  const path = params.path?.join("/") || "";
  const sourceUrl = new URL(request.url);
  const targetUrl = new URL(`/api/${path}${sourceUrl.search}`, getAiUgcBaseUrl());

  const headers = new Headers();
  const contentType = request.headers.get("content-type");
  const cookie = request.headers.get("cookie");
  const accept = request.headers.get("accept");

  if (contentType) headers.set("content-type", contentType);
  if (cookie) headers.set("cookie", cookie);
  if (accept) headers.set("accept", accept);

  const init: RequestInit = {
    method: request.method,
    headers,
    redirect: "manual",
  };

  if (!["GET", "HEAD"].includes(request.method)) {
    init.body = await request.arrayBuffer();
  }

  const response = await fetch(targetUrl, init);
  const responseHeaders = new Headers(response.headers);
  responseHeaders.delete("content-encoding");
  responseHeaders.delete("transfer-encoding");

  return new NextResponse(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers: responseHeaders,
  });
}

export async function GET(request: NextRequest, context: RouteContext) {
  return proxyAiUgcRequest(request, context);
}

export async function POST(request: NextRequest, context: RouteContext) {
  return proxyAiUgcRequest(request, context);
}

export async function PATCH(request: NextRequest, context: RouteContext) {
  return proxyAiUgcRequest(request, context);
}

export async function DELETE(request: NextRequest, context: RouteContext) {
  return proxyAiUgcRequest(request, context);
}
