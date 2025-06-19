%% Supervise the preparation of gaze coded data for iCatcher training
% This script is given a string to pattern match to and then finds the movie files and coder files and moves them to the destination folder, ready for running iCatcher's preprocess
% This is intelligent about deciding who should be first and second coder: it will check the coder has an appropriate name (not 'FSpot' or 'Test'), then it will check that the coders finished, then it will randomly pick the first coder.

function supervisor_iCatcher_prep(pattern_str, output_dir)

% Specify the directory where the frames and coder files are located
videos_path = '/home/cte/Desktop/Experiments/soc_neuropipe_private/scripts/Gaze_Categorization/Frames/';
coders_path = '/home/cte/Desktop/Experiments/soc_neuropipe_private/scripts/Gaze_Categorization/Coder_Files/';

% Where are the output files going to be saved?
coder_directories = {'coding_first', 'coding_second'};

% Check if the output directory exists, if not create it
if ~exist(output_dir, 'dir')
    mkdir(output_dir);
    mkdir(fullfile(output_dir, coder_directories{1}));
    mkdir(fullfile(output_dir, coder_directories{2}));
    mkdir(fullfile(output_dir, 'videos'));
end

% Get the files in the video directory that match the pattern
video_files = dir(fullfile(videos_path, [pattern_str, '*']));

% Loop through the video file
for ppt_counter = 1:length(video_files)
    ppt = video_files(ppt_counter).name;
    
    % Find the corresponding coder files
    coder_files = dir(fullfile(coders_path, [ppt, '_*.mat']));
    
    if isempty(coder_files)
        fprintf('No coder files found for %s\n', ppt);
        continue;
    end
    
    % Check if the coder files are valid
    valid_coders = {};
    for coder_counter = 1:length(coder_files)
        coder_name = coder_files(coder_counter).name(1:end-4); % Remove .mat extension
        if contains(coder_name, 'Coder_FSpot') && contains(coder_name, 'Coder_Test')
            valid_coders{end+1} = coder_name; %#ok<AGROW>
        end
    end
    
    if isempty(valid_coders)
        fprintf('No valid coders found for %s\n', ppt);
        continue;
    end
    
    % Loop through the coders and check that they finished
    finished_coders = {};
    for coder_counterj = 1:length(valid_coders)
        coder_name = valid_coders{coder_counter};
        coder_file = fullfile(coders_path, [coder_name, '.mat']);
        
        coder_data = load(coder_file);

        if length(coder_data.Output.FrameName) > 1000
            finished_coders{end+1} = coder_name; %#ok<AGROW>
        end
        
    end
    if isempty(finished_coders)
        fprintf('No finished coders found for %s\n', ppt);
        continue;
    end

    % Randomly select a first coder
    ordered_coders = finished_coders(randperm(length(finished_coders))); 
    
    % Check if there are more than two coders
    if length(ordered_coders) > 2
        ordered_coders = ordered_coders{1:2}; % Only take the first two coders
        fprintf('More than two coders found for %s, using first two: %s and %s\n', ppt, ordered_coders{1}, ordered_coders{2});
        continue
    end

    % Copy over the coder file to the output directory
    for coder_counter = 1:length(ordered_coders)
        coder_name = ordered_coders{coder_counter};
        coder_file = fullfile(coders_path, [coder_name, '.mat']);
        
        % What is the 
        output_file = fullfile(output_dir, coder_directories{coder_counter}, [ppt, '.csv']);

        % Run the conversion function to convert the coder file to the output format
        convert_Lookit_coder_file(coder_file, output_file);
        fprintf('Copied coder file %s to %s\n', coder_file, output_file);
    end

    % Copy the video file to the output directory
    video_file_path = fullfile(videos_path, video_file);

    % Get all the videos in that directory
    potential_videos = dir([video_file_path, '/*.mp4']);


    % Take the last video in the list
    if ~isempty(potential_video_names)
        % Sort the videos by name 
        potential_video_names = sort({potential_videos.name});

        % Get the last video in the list
        last_video = potential_video_names{end};

        % Copy the video file to the output directory
        output_video_file = fullfile(output_dir, 'videos', [ppt, '.mp4']);
        copyfile(fullfile(videos_path, last_video), output_video_file);
    else
        fprintf('No videos found for %s\n', ppt);
        continue;
    end

end

end


