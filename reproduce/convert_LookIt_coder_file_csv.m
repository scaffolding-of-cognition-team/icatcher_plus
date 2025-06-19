% Convert a given coder file into a csv
% Input:
% coder_file: the file path of the coder file
% output_csv: what is the path to the output csv file
% Output:
% Each row corresponds to a frame of the video, whether or not it was manually coded
% First column is the frame name, as stored in the eyeImages folder
% Second column is the code the manual coder assigned to the frame

function convert_LookIt_coder_file_csv(coder_file, output_csv)
    
    % Load in the coder file. The only variable stored is the Output variable
    load(coder_file);

    % Get a cell with all the frame names
    Frame_names = Output.FrameName;

    % Get the behavioral report for each frame
    Codes = Output.Experiment;

    % Check if the coder finished
    if (exist('FrameList') == 0) || (length(Frame_names) < length(FrameList))
        fprintf('%s only finished %d of %d frames. Quitting without returning output\n', coder_file, length(Frame_names), length(FrameList))
        return
    end
    
    % Open the output csv file
    fid = fopen(output_csv, 'w');

    % Loop through each frame and get the time point and code
    for frame_counter = 1:length(Frame_names)
        % Get the frame name, ignoring the path and extension
        [~, frame_name, ~] = fileparts(Frame_names{frame_counter});
        
        frame_numbers = str2double(regexp(frame_name, '\d{6}', 'match', 'once'));
        frame_str = sprintf('%d%d ', frame_numbers - 1);
        % Get the code assigned to the frame
        code = Codes{frame_counter};
           
        if strcmp(code,'a')
            code = 'left';
        elseif strcmp(code, 'd')
            code = 'right';
        elseif strcmp(code, 's')
            code = 'center';
        elseif strcmp(code, 'space')
            code = 'away';
        else
            code = 'none';
        end
        % Write the time point and code to the csv file
        fprintf(fid, '%s,%s\n', frame_str, code);
    end
    
    % Close the csv
    fclose(fid);
end
