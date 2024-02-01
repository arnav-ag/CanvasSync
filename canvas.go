package main

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
)

const (
  baseURL     = "https://canvas.nus.edu.sg"
	baseDir     = "/Users/arnav/Documents/NUS/canvas/test"
	pageLimit   = "100"
	coursesJSON = `[
		{"id": 52858, "name": "CS3211 Parallel and Concurrent Programming [2320]"},
		{"id": 52872, "name": "CS3223 Database Systems Implementation [2320]"},
		{"id": 52937, "name": "CS4215 Programming Language Implementation [2320]"},
		{"id": 52975, "name": "CS4247 Graphics Rendering Techniques [2320]"},
		{"id": 52980, "name": "CS4248 Natural Language Processing [2320]"},
		{"id": 53069, "name": "CS5331 Web Security [2320]"},
		{"id": 55884, "name": "ST3131 Regression Analysis [2320]"}
	]`
)

type Course struct {
	ID   int    `json:"id"`
	Name string `json:"name"`
}

type Folder struct {
	ID            int    `json:"id"`
	FullName      string `json:"full_name"`
	FilesURL      string `json:"files_url"`
	Name          string `json:"name"`
}

type File struct {
	ID            int    `json:"id"`
	DisplayName   string `json:"display_name"`
	URL           string `json:"url"`
	UpdatedAt     string `json:"updated_at"`
}

func main() {
	var courses []Course
	err := json.Unmarshal([]byte(coursesJSON), &courses)
	if err != nil {
		fmt.Printf("Error parsing courses JSON: %v\n", err)
		return
	}

	var wg sync.WaitGroup
	for _, course := range courses {
		wg.Add(1)
		go func(course Course) {
			defer wg.Done()
			handleCourse(course)
		}(course)
	}
	wg.Wait()
}


func handleCourse(course Course) {
    foldersURL := fmt.Sprintf("%s/api/v1/courses/%d/folders?per_page=%s", baseURL, course.ID, pageLimit)
    client := &http.Client{}
    req, _ := http.NewRequest("GET", foldersURL, nil)
    req.Header.Add("Authorization", "Bearer "+token)

    resp, err := client.Do(req)
    if err != nil {
        fmt.Printf("Error fetching folders for course %s: %v\n", course.Name, err)
        return
    }
    defer resp.Body.Close()

    var folders []Folder
    if err := json.NewDecoder(resp.Body).Decode(&folders); err != nil {
        fmt.Printf("Error decoding folder response for course %s: %v\n", course.Name, err)
        return
    }

    for _, folder := range folders {
        handleFolder(course, folder)
    }
}

func handleFolder(course Course, folder Folder) {
    client := &http.Client{}
    req, err := http.NewRequest("GET", folder.FilesURL+"?per_page="+pageLimit, nil)
    if err != nil {
        fmt.Printf("Error creating request for folder %s: %v\n", folder.FullName, err)
        return
    }
    req.Header.Add("Authorization", "Bearer "+token)

    resp, err := client.Do(req)
    if err != nil {
        fmt.Printf("Error fetching files for folder %s: %v\n", folder.FullName, err)
        return
    }
    defer resp.Body.Close()

    if resp.StatusCode != http.StatusOK {
        bodyBytes, _ := io.ReadAll(resp.Body)
        fmt.Printf("Unexpected response for folder %s: %s\n", folder.FullName, string(bodyBytes[:200]))
        return
    }

    var files []File
    if err := json.NewDecoder(resp.Body).Decode(&files); err != nil {
        fmt.Printf("Error decoding files response for folder %s: %v\n", folder.FullName, err)
        return
    }

    // Define a concurrency limit
    const maxConcurrentDownloads = 20
    semaphore := make(chan struct{}, maxConcurrentDownloads)

    var wg sync.WaitGroup
    for _, file := range files {
        wg.Add(1)
        semaphore <- struct{}{} // Acquire semaphore
        go func(file File) {
            defer wg.Done()
            downloadFile(course, folder.FullName, file)
            <-semaphore // Release semaphore
        }(file)
    }
    wg.Wait()
}

func downloadFile(course Course, folderName string, file File) {
    url := file.URL
    fileName := file.DisplayName

    folderPath := strings.TrimPrefix(folderName, "course files/")
    savePath := filepath.Join(baseDir, course.Name, folderPath)
    os.MkdirAll(savePath, os.ModePerm)

    saveFilePath := filepath.Join(savePath, fileName)
    fmt.Printf("Downloading %s to %s\n", fileName, saveFilePath)

	resp, err := http.Get(url) // No auth token required for direct file URLs
	if err != nil {
		fmt.Printf("Error downloading file %s: %v\n", fileName, err)
		return
	}
	defer resp.Body.Close()

	fileOut, err := os.Create(saveFilePath)
	if err != nil {
		fmt.Printf("Error creating file %s: %v\n", saveFilePath, err)
		return
	}
	defer fileOut.Close()

	_, err = io.Copy(fileOut, resp.Body)
	if err != nil {
		fmt.Printf("Error writing file %s: %v\n", saveFilePath, err)
		return
	}
}

