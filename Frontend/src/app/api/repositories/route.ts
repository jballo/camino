import { NextResponse } from "next/server";

export async function POST(req: Request) {
  try {
    const body = await req.json();
    const { url } = body;

    if (!url || url.length === 0) throw new Error(`Failure processin`);

    console.log("Repo url: ", url);
    return new NextResponse(`Success processing ${url}`, { status: 200 });
  } catch (error) {
    console.error("[/api/repositories]: ", error);
    return new Response(`System failed`, { status: 500 });
  }
}
