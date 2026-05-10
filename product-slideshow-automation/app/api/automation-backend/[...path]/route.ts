import { NextRequest, NextResponse } from "next/server";

type RouteContext = {
  params: Promise<{
    path?: string[];
  }>;
};

function getAutomationBackendBaseUrl(): string {
  const baseUrl = process.env.AUTOMATION_BACKEND_URL || process.env.NEXT_PUBLIC_AUTOMATION_BACKEND_URL || "";
  return baseUrl.replace(/\/$/, "");
}

async function proxyAutomationBackendRequest(request: NextRequest, context: RouteContext) {
  const baseUrl = getAutomationBackendBaseUrl();
  const accessToken = process.env.AUTOMATION_BACKEND_ACCESS_TOKEN || "";

  if (!baseUrl) {
    return NextResponse.json({ detail: "AUTOMATION_BACKEND_URL is not configured." }, { status: 500 });
  }

  if (!accessToken) {
    return NextResponse.json({ detail: "AUTOMATION_BACKEND_ACCESS_TOKEN is not configured." }, { status: 500 });
  }

  const params = await context.params;
  const path = params.path?.join("/") || "";
  const sourceUrl = new URL(request.url);
  const targetUrl = new URL(`/${path}${sourceUrl.search}`, baseUrl);

  const headers = new Headers();
  const contentType = request.headers.get("content-type");
  const accept = request.headers.get("accept");

  if (contentType) headers.set("content-type", contentType);
  if (accept) headers.set("accept", accept);
  headers.set("authorization", `Bearer ${accessToken}`);

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
  return proxyAutomationBackendRequest(request, context);
}

export async function POST(request: NextRequest, context: RouteContext) {
  return proxyAutomationBackendRequest(request, context);
}

export async function PATCH(request: NextRequest, context: RouteContext) {
  return proxyAutomationBackendRequest(request, context);
}

export async function DELETE(request: NextRequest, context: RouteContext) {
  return proxyAutomationBackendRequest(request, context);
}
