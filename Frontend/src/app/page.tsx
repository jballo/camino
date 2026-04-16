"use client";

import Header from "@/components/header";
import { Button, Input, Textarea } from "@headlessui/react";
import { ArrowUp, Link } from "lucide-react";
import { useCallback, useState } from "react";

export default function Home() {
  const [url, setUrl] = useState("");
  const [prompt, setPrompt] = useState("");

  const onSubmitRepo = useCallback(async () => {
    try {
      console.log("url: ", url);
      if (url.length == 0) throw new Error("Invalid");
      const response = await fetch("/api/repositories", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          repoUrl: url,
        }),
      });

      if (!response.ok) throw new Error("Error");

      const result = await response.text();
      console.log("Result: ", result);
    } catch (error) {
      console.log("error: ", error);
    }
  }, [url]);

  const onSubmitPrompt = useCallback(async () => {
    console.log("url: ", url);
    console.log("prompt: ", prompt);

    try {
      const response = await fetch("/api/journeys", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          repoUrl: url,
          prompt,
        }),
      });

      if (!response.ok) throw new Error("Error");

      const result = await response.text();
      console.log("Result: ", result);
    } catch (error) {
      console.log("error: ", error);
    }
  }, [url, prompt]);

  return (
    <div className="flex flex-col w-full h-screen">
      <div className="flex w-full h-1/12">
        <Header />
      </div>
      <div className="flex flex-col justify-center items-center w-full h-11/12">
        <div className="flex flex-col justify-center items-center w-2/3 h-1/4 gap-4 -mt-40">
          <div className="flex justify-center">
            <h2 className="text-4xl">Peep into a Codebase</h2>
          </div>
          <div className="flex w-full gap-4 items-center">
            <div className="flex flex-row outline-1 outline-accent rounded-2xl p-3 gap-4 w-2/3">
              <div className="flex items-center">
                <Link className="w-4 h-4" />
              </div>
              <Input
                placeholder="https://github.com/..."
                className="focus:outline-none w-115"
                value={url}
                onChange={(e) => setUrl(e.target.value)}
              />
            </div>
            <Button
              className="flex h-10 bg-primary rounded-md justify-center items-center p-2 text-primary-foreground"
              aria-label="Process"
              onClick={async () => {
                await onSubmitRepo();
              }}
            >
              Process
            </Button>
          </div>
          <div className="flex flex-col outline-1 outline-accent rounded-2xl p-5 gap-7 w-full max-w-[1000px]">
            <Textarea
              placeholder="What do you want to know?"
              className="w-full text-start focus:outline-none field-sizing-content max-h-40 resize-none"
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
            />
            <div className="flex w-full justify-between">
              <p>(:</p>
              <Button
                className="flex w-7 h-7 bg-primary rounded-md justify-center items-center text-primary-foreground"
                aria-label="Submit"
                onClick={async () => await onSubmitPrompt()}
              >
                <div>
                  <ArrowUp aria-hidden="true" />
                </div>
              </Button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
