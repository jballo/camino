"use client";

import Header from "@/components/header";
import { Button, Label, Radio, RadioGroup, Textarea } from "@headlessui/react";
import { ArrowUp, CheckCircleIcon } from "lucide-react";
import { useCallback, useState, useTransition } from "react";
import {
  Description,
  Dialog,
  DialogPanel,
  DialogTitle,
} from "@headlessui/react";

const sampleRepos: { id: string; repoName: string }[] = [
  { id: "abc", repoName: "RepositoryABC" },
  { id: "lmn", repoName: "RepositoryLMN" },
  { id: "xyz", repoName: "RepositoryXYZ" },
];

export default function Home() {
  const [prompt, setPrompt] = useState("");
  const [repoSelectionDialog, setRepoSelectionDialog] = useState(false);
  const [repoSelected, setRepoSelected] = useState<undefined | string>(
    undefined,
  );
  const [repos, setRepos] = useState<string[]>([]);
  const [repoRetrievalError, setRepoRetrievalError] = useState<
    string | undefined
  >(undefined);
  const [isPending, startTransition] = useTransition();

  const onSubmitPrompt = useCallback(async () => {
    try {
      if (
        prompt.length <= 0 ||
        repoSelected == undefined ||
        repoSelected.length <= 0
      )
        throw new Error(`Invalid input`);

      const response = await fetch("/api/journeys", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          url: repoSelected,
          prompt,
        }),
      });

      if (!response.ok) throw new Error("Error");

      const result = await response.text();
      console.log("Result: ", result);
    } catch (error) {
      console.log("error: ", error);
    }
  }, [repoSelected, prompt]);

  const openDialog = async () => {
    setRepoSelectionDialog(true);
    setRepoRetrievalError(undefined);
    startTransition(async () => {
      try {
        const response = await fetch("/api/repositories", {
          method: "GET",
        });

        if (!response.ok) throw new Error("Failed to get repos");
        const result = await response.json();

        setRepos(result);
      } catch (error) {
        console.log("Error: ", error);
        setRepoRetrievalError("Failed to retrieve repositories");
      }
    });
  };

  const processRepo = useCallback(async () => {
    console.log("repo selected: ", repoSelected);
    if (repoSelected == undefined) return;

    try {
      const response = await fetch("/api/repositories/ingest", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          repoName: repoSelected,
        }),
      });

      if (!response.ok) throw new Error("Failed to process repo");

      const result = await response.text();
      console.log("Result: ", result);
      setRepoSelectionDialog(false);
    } catch (error) {
      console.log("Error: ", error);
    }
  }, [repoSelected, repoSelectionDialog]);

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
          <div className="flex flex-col outline-1 outline-accent rounded-2xl p-5 gap-7 w-full max-w-[1000px]">
            <Textarea
              placeholder="What do you want to know?"
              className="w-full text-start focus:outline-none field-sizing-content max-h-40 resize-none"
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
            />
            <div className="flex w-full justify-between">
              <Button onClick={openDialog}>Select Repo</Button>
              <Dialog
                open={repoSelectionDialog}
                onClose={() => setRepoSelectionDialog(false)}
                className="relative z-50"
              >
                <div className="fixed inset-0 flex w-screen items-center justify-center p-4">
                  <DialogPanel className="max-w-lg space-y-4 border bg-primary-foreground p-12 rounded-md">
                    <DialogTitle className="font-bold">
                      Select Repository
                    </DialogTitle>
                    <Description>
                      This will be the repository the journeys will be based on.
                    </Description>
                    <RadioGroup
                      value={repoSelected || ""}
                      onChange={setRepoSelected}
                      className="flex flex-col gap-3"
                    >
                      {repoRetrievalError && <div>{repoRetrievalError}</div>}
                      {isPending && <div>Loading...</div>}
                      {repos.map((repo) => (
                        <Radio
                          key={repo}
                          value={repo}
                          className="group flex flex-row items-center justify-between relative data-checked:bg-secondary h-10 p-3 rounded-sm"
                        >
                          <Label>{repo}</Label>
                          <CheckCircleIcon className="size-5 fill-white opacity-0 transition group-data-checked:opacity-100" />
                        </Radio>
                      ))}
                    </RadioGroup>
                    <div className="flex gap-4">
                      <Button
                        className="w-20 h-6 rounded-sm"
                        onClick={() => setRepoSelectionDialog(false)}
                      >
                        Cancel
                      </Button>
                      <Button
                        className="bg-primary text-white w-20 h-6 rounded-sm"
                        onClick={() => {
                          processRepo();
                        }}
                      >
                        Process
                      </Button>
                    </div>
                  </DialogPanel>
                </div>
              </Dialog>
              <Button
                className="flex w-7 h-7 bg-primary rounded-md justify-center items-center text-primary-foreground"
                aria-label="Submit"
                onClick={onSubmitPrompt}
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
